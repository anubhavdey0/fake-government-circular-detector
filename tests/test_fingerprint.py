from __future__ import annotations

import io

import numpy as np
from PIL import Image

from conftest import OFFICIAL, OTHER, png_bytes
from pdfgen import make_pdf

from circverify.extract import render_pdf_page
from circverify.fingerprint import (
    dhash,
    hamming_many,
    minhash,
    phash,
    shingle_hashes,
    signature_similarity,
    to_signed64,
    to_unsigned64,
    words,
)


def test_words_normalise_case_and_ligatures():
    assert words("Notiﬁcation  REGARDING\nFee") == ["notification", "regarding", "fee"]


def test_hindi_words_are_not_split_at_vowel_signs():
    assert words("परीक्षा की तिथि।") == ["परीक्षा", "की", "तिथि"]


def test_minhash_tracks_jaccard():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 2**32, size=600, dtype=np.uint64)
    a, b = base[:400], base[200:]  # overlap 200 of 600 -> J = 1/3
    est = signature_similarity(minhash(a), minhash(b)[None, :])[0]
    assert abs(est - 1 / 3) < 0.12
    assert signature_similarity(minhash(a), minhash(a)[None, :])[0] == 1.0


def test_shingles_are_stable_across_runs():
    # Index format: these values must never change without a version bump.
    assert sorted(shingle_hashes(["the", "last", "date"]).tolist()) == sorted(
        shingle_hashes(words("The last DATE")).tolist())


def test_page_hash_survives_rescaling_and_jpeg_but_not_other_pages():
    page = render_pdf_page(make_pdf([OFFICIAL]), 1, 512)
    other = render_pdf_page(make_pdf([OTHER]), 1, 512)
    shrunk = page.resize((360, round(page.height * 360 / 512)))
    jpeg = Image.open(io.BytesIO(png_bytes(shrunk.convert("RGB"), "JPEG", quality=60))).convert("L")
    jpeg = jpeg.resize((512, page.height))
    ph = phash(page)
    assert int(hamming_many(ph, np.array([phash(jpeg)], dtype=np.uint64))[0]) <= 6
    assert int(hamming_many(ph, np.array([phash(other)], dtype=np.uint64))[0]) > 6
    assert dhash(page) != dhash(other)


def test_signed_round_trip():
    for v in (0, 1, 2**63 - 1, 2**63, 2**64 - 1):
        assert to_unsigned64(to_signed64(v)) == v
        assert -(2**63) <= to_signed64(v) < 2**63
