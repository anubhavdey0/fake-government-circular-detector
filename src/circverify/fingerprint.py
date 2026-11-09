"""Text and image fingerprints.

Text: word 3-gram shingles hashed to 32 bits, summarised with a 128-way
MinHash so near-duplicate pages can be found without comparing full texts.

Images: a 64-bit DCT perceptual hash and a 64-bit difference hash of each
rendered page, which survive rescaling and JPEG recompression (a WhatsApp
forward) but not edits to the layout.

Every constant here is part of the index format. Changing one means
re-indexing, so bump ``FINGERPRINT_VERSION`` with it.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

import numpy as np
from PIL import Image

FINGERPRINT_VERSION = 1
SHINGLE_SIZE = 3
NUM_PERM = 128

# Words: letters/digits plus the Indic blocks (whose vowel signs are not
# matched by \w), excluding the danda punctuation marks U+0964/U+0965.
_WORD = re.compile(r"[\wऀ-ॣ०-෿]+", re.UNICODE)

_MERSENNE = np.uint64((1 << 61) - 1)
_MASK32 = np.uint64(0xFFFFFFFF)
_rng = np.random.RandomState(20261109)  # legacy RandomState: stable across NumPy versions
_A = _rng.randint(1, 2**32, size=NUM_PERM, dtype=np.uint64)
_B = _rng.randint(0, 2**32, size=NUM_PERM, dtype=np.uint64)
EMPTY_SIGNATURE = np.full(NUM_PERM, 0xFFFFFFFF, dtype=np.uint32)


# -- text ------------------------------------------------------------------

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def word_spans(text: str) -> list[tuple[str, int, int]]:
    """Words of ``text`` with their character offsets (text must already be NFKC)."""
    return [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]


def words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(nfkc(text))]


def shingle_hashes(tokens: list[str], k: int = SHINGLE_SIZE) -> np.ndarray:
    """Distinct 32-bit hashes of the k-word shingles (as uint64 for the arithmetic below)."""
    if not tokens:
        return np.empty(0, dtype=np.uint64)
    if len(tokens) < k:
        grams = [" ".join(tokens)]
    else:
        grams = [" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)]
    hashes = {int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=4).digest(), "little")
              for g in grams}
    return np.fromiter(hashes, dtype=np.uint64, count=len(hashes))


def minhash(hashes: np.ndarray) -> np.ndarray:
    """128 independent minimums of (a*x + b) mod p. Inputs are < 2**32, so nothing overflows."""
    if hashes.size == 0:
        return EMPTY_SIGNATURE.copy()
    sig = np.full(NUM_PERM, np.iinfo(np.uint64).max, dtype=np.uint64)
    for start in range(0, hashes.size, 4096):
        block = hashes[start : start + 4096]
        values = (np.outer(block, _A) + _B) % _MERSENNE & _MASK32
        np.minimum(sig, values.min(axis=0), out=sig)
    return sig.astype(np.uint32)


def signature_similarity(sig: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Estimated Jaccard similarity of ``sig`` against every row of ``matrix``."""
    return (matrix == sig).mean(axis=1)


def exact_shingles(text: str) -> set[int]:
    return set(shingle_hashes(words(text)).tolist())


# -- images ----------------------------------------------------------------

def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2 / n)
    m[0] /= np.sqrt(2)
    return m


_DCT32 = _dct_matrix(32)
_BIT_WEIGHTS = (np.uint64(1) << np.arange(64, dtype=np.uint64))


def _bits_to_int(bits: np.ndarray) -> int:
    return int((bits.astype(np.uint64).ravel() * _BIT_WEIGHTS).sum())


def phash(img: Image.Image) -> int:
    a = np.asarray(img.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    low = (_DCT32 @ a @ _DCT32.T)[:8, :8].ravel()
    return _bits_to_int(low > np.median(low[1:]))


def dhash(img: Image.Image) -> int:
    a = np.asarray(img.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    return _bits_to_int(a[:, 1:] > a[:, :-1])


def ink_ratio(img: Image.Image) -> float:
    """Share of dark pixels at hashing resolution. Near-blank pages get no visual matching (they all look alike).

    Measured on the 512 px render itself: shrinking further averages thin
    scanned strokes into grey and makes real pages look blank.
    """
    g = img.convert("L")
    if g.width > 1024:
        g = g.resize((512, max(1, round(g.height * 512 / g.width))), Image.Resampling.LANCZOS)
    return float((np.asarray(g) < 128).mean())


HEADER_SHARE = 0.22  # top of the first page: crest, logos, office name and address


def header_fingerprint(image: Image.Image) -> tuple[int, float]:
    """Perceptual hash and ink share of the letterhead band at the top of a page."""
    band = image.convert("L").crop((0, 0, image.width, max(1, int(image.height * HEADER_SHARE))))
    return to_signed64(phash(band)), ink_ratio(band)


def to_signed64(value: int) -> int:
    """SQLite integers are signed 64-bit."""
    return value - (1 << 64) if value >= (1 << 63) else value


def to_unsigned64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def hamming_many(value: int, arr: np.ndarray) -> np.ndarray:
    return np.bitwise_count(arr ^ np.uint64(value)).astype(np.int16)


def page_fingerprint(text: str, image: Image.Image | None) -> dict:
    """All per-page fingerprints in the shape stored in the ``pages`` table."""
    hashes = shingle_hashes(words(text)) if text else np.empty(0, dtype=np.uint64)
    fp = {
        "n_shingles": int(hashes.size),
        "minhash": minhash(hashes).tobytes() if hashes.size else None,
        "phash": None,
        "dhash": None,
        "ink": None,
    }
    if image is not None:
        fp["phash"] = to_signed64(phash(image))
        fp["dhash"] = to_signed64(dhash(image))
        fp["ink"] = ink_ratio(image)
    return fp
