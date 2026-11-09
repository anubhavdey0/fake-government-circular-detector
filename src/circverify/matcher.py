"""Find the official document a submission came from, and say how it differs.

Pipeline:

1. SHA-256 lookup. A byte-identical file is the strongest answer there is.
2. Candidate retrieval, three ways at once: MinHash similarity of each page's
   word 3-grams, a full-text query on the page's longest words, and
   perceptual-hash distance of the rendered page.
3. Exact re-scoring of the best candidates: share of the submission's
   3-grams that occur in the official text, page by page.
4. Detailed comparison of the top few: word-level text diff and pixel diff
   of each aligned page pair.
5. Verdict from the evidence. There is no classifier and no notion of what a
   forgery "looks like"; the only reference is what the issuer published.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np
from PIL import Image

from .db import Database, Provenance
from .diff import DIFF_WIDTH, TextComparison, VisualDiff, compare_images, compare_text
from .extract import HASH_WIDTH, UnsupportedFile, iter_pages, load_image, pdf_info, render_pdf_page, sniff_kind
from .fingerprint import (
    NUM_PERM,
    hamming_many,
    ink_ratio,
    minhash,
    phash,
    shingle_hashes,
    signature_similarity,
    to_unsigned64,
    words,
)
from .forensics import compare_signals, pdf_signals
from .ocr import load_ocr
from .store import BlobStore

VERIFIED_EXACT = "VERIFIED_EXACT"
VERIFIED_CONTENT = "VERIFIED_CONTENT"
ALTERED = "ALTERED"
NO_MATCH = "NO_MATCH"
UNREADABLE = "UNREADABLE"

MAX_QUERY_PAGES = 30
MAX_VISUAL_PAGES = 6
MAX_RESCORE = 15
MAX_DETAILED = 3
MIN_QUERY_SHINGLES = 8
TEXT_MATCH = 0.5  # share of the submission's word 3-grams found in the official text
PAGE_TEXT_MATCH = 0.3
VISUAL_MAX_HAMMING = 14
MIN_INK = 0.003
LOCALIZED_CHANGE = 0.10
ROTATION_GATE = 10  # pHash distance a rotated picture must reach to be compared in full


@dataclass
class QueryPage:
    number: int
    text: str
    text_source: str
    image: Image.Image | None
    shingles: set[int]
    signature: np.ndarray | None
    phash: int | None
    ink: float | None


@dataclass
class Match:
    sha256: str
    provenance: list[Provenance]
    n_pages: int | None
    page_map: dict[int, int] = field(default_factory=dict)
    unmatched_pages: list[int] = field(default_factory=list)
    text_containment: float | None = None
    text: TextComparison | None = None
    visual: list[VisualDiff] = field(default_factory=list)
    official_signals: dict | None = None

    @property
    def visual_strength(self) -> float:
        same = [1 - v.raw_changed_ratio for v in self.visual if v.same_document]
        return max(same) if same else 0.0

    @property
    def strength(self) -> float:
        return max(self.text_containment or 0.0, self.visual_strength)

    @property
    def primary(self) -> Provenance | None:
        return self.provenance[0] if self.provenance else None

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "official": [asdict(p) for p in self.provenance],
            "n_pages": self.n_pages,
            "page_map": {str(k): v for k, v in self.page_map.items()},
            "unmatched_pages": self.unmatched_pages,
            "text_containment": None if self.text_containment is None else round(self.text_containment, 4),
            "text_similarity": None if self.text is None else round(self.text.ratio, 4),
            "text_changes": [c.to_dict() for c in self.text.changes] if self.text else [],
            "visual": [v.to_dict() for v in self.visual],
        }


@dataclass
class VerificationResult:
    verdict: str
    summary: str
    sha256: str
    filename: str | None = None
    kind: str | None = None
    n_pages: int | None = None
    text_source: str | None = None
    match: Match | None = None
    alternatives: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    search_hint: str = ""  # the submission's subject line, to prefill a search for the real notice

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "file": {"name": self.filename, "sha256": self.sha256, "kind": self.kind,
                     "pages": self.n_pages, "text_source": self.text_source},
            "match": self.match.to_dict() if self.match else None,
            "alternatives": self.alternatives,
            "notes": self.notes,
            "signals": self.signals,
            "index": self.index,
        }


class SignatureIndex:
    """Every indexed page's fingerprints, held in arrays for vectorised comparison."""

    def __init__(self, db: Database, exclude: frozenset[str] = frozenset()):
        rows = [r for r in db.conn.execute(
            "SELECT sha256, page_no, n_shingles, minhash, phash, ink FROM pages ORDER BY sha256, page_no")
            if r[0] not in exclude]
        n = len(rows)
        self.sha = np.array([r[0] for r in rows], dtype=object)
        self.page_no = np.array([r[1] for r in rows], dtype=np.int32)
        self.n_shingles = np.array([r[2] for r in rows], dtype=np.int32)
        self.sigs = np.zeros((n, NUM_PERM), dtype=np.uint32)
        for i, r in enumerate(rows):
            if r[3]:
                self.sigs[i] = np.frombuffer(r[3], dtype=np.uint32)
        self.phash = np.array([to_unsigned64(r[4]) if r[4] is not None else 0 for r in rows], dtype=np.uint64)
        ink = np.array([r[5] if r[5] is not None else 0.0 for r in rows], dtype=np.float64)
        self.visual_ok = np.array([r[4] is not None for r in rows], dtype=bool) & (ink >= MIN_INK)
        self.text_ok = self.n_shingles > 0
        self.rows_by_sha: dict[str, np.ndarray] = {}
        groups: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            groups[r[0]].append(i)
        self.rows_by_sha = {k: np.array(v) for k, v in groups.items()}

    def __len__(self) -> int:
        return len(self.sha)


class Verifier:
    def __init__(self, db: Database, store: BlobStore, *, use_ocr: bool | None = None,
                 exclude: frozenset[str] | set[str] = frozenset(), ocr=None):
        self.db = db
        self.store = store
        self.exclude = frozenset(exclude)
        self.ocr = ocr if ocr is not None else load_ocr(use_ocr)
        self.index = SignatureIndex(db, self.exclude)
        self._prep_notes: list[str] = []

    # -- public ---------------------------------------------------------------

    def verify(self, data: bytes, filename: str | None = None, *,
               exclude: set[str] | frozenset[str] | None = None) -> VerificationResult:
        """Check one document. ``exclude`` hides files from this call only (used for hold-out tests)."""
        excluded = self.exclude | frozenset(exclude or ())
        sha = hashlib.sha256(data).hexdigest()
        kind = sniff_kind(data[:2048])
        res = VerificationResult(verdict=UNREADABLE, summary="", sha256=sha, filename=filename, kind=kind,
                                 index=self._index_info())
        if kind is None:
            res.summary = "Not a PDF or a supported image (PNG, JPEG, WebP, GIF, TIFF)."
            return res

        if sha not in excluded:
            prov = self.db.provenance(sha)
            if prov:
                row = self.db.file(sha)
                res.verdict = VERIFIED_EXACT
                res.match = Match(sha, prov, row["n_pages"] if row else None, text_containment=1.0)
                res.n_pages = res.match.n_pages
                p = prov[0]
                res.summary = (f"Byte-for-byte identical to a file the issuer published: “{p.title}”"
                               f"{_dated(p.published)}.")
                res.notes += _provenance_notes(prov)
                return res

        try:
            pages, n_pages, signals = self._prepare(data, kind)
        except UnsupportedFile as exc:
            res.summary = f"Could not read the file: {exc}"
            return res
        res.n_pages = n_pages
        res.signals = signals
        sources = {p.text_source for p in pages}
        res.text_source = "layer" if "layer" in sources else ("ocr" if "ocr" in sources else "none")
        res.search_hint = _subject_line(pages)

        extra = excluded - self.exclude
        text_floor = {"layer": PAGE_TEXT_MATCH, "ocr": 0.15}.get(res.text_source)
        detailed = self._match(pages, extra, text_floor)
        best = detailed[0] if detailed else None
        if kind != "pdf" and pages and pages[0].image is not None and not _confirmed(best, res.text_source):
            # A sideways or upside-down picture always finds some lookalike on the shared
            # letterhead, so retry the other orientations whenever nothing is confirmed.
            upright = self._closest_page_hash(pages)
            for angle in (90, 270, 180):
                turned = [self._query_page(p.number, p.text, p.text_source, p.image.rotate(angle, expand=True))
                          for p in pages]
                # Cheap gate: only run the full comparison if this orientation looks clearly closer.
                if self._closest_page_hash(turned) > min(ROTATION_GATE, upright - 4):
                    continue
                tried = self._match(turned, extra, text_floor)
                if tried and _confirmed(tried[0], res.text_source):
                    pages, detailed, best = turned, tried, tried[0]
                    res.notes.append(f"The picture was rotated by {angle} degrees and was turned upright to compare.")
                    break
        res.notes.extend(self._prep_notes)
        res.alternatives = [
            {"sha256": m.sha256, "title": m.primary.title if m.primary else None,
             "published": m.primary.published if m.primary else None,
             "url": m.primary.url if m.primary else None, "strength": round(m.strength, 3)}
            for m in detailed[1:] if m.strength >= TEXT_MATCH
        ]
        self._decide(res, pages, best)
        return res

    # -- stages ---------------------------------------------------------------

    def _closest_page_hash(self, pages: list[QueryPage]) -> int:
        idx = self.index
        best = 64
        for p in pages:
            if p.phash is not None and (p.ink or 0) >= MIN_INK and len(idx):
                d = hamming_many(p.phash, idx.phash)
                d[~idx.visual_ok] = 99
                best = min(best, int(d.min()))
        return best

    def _match(self, pages: list[QueryPage], extra: frozenset[str], text_floor: float | None) -> list[Match]:
        candidates = self._rescore(pages, self._candidates(pages, extra))
        detailed = []
        for s in candidates[:MAX_DETAILED]:
            m = self._compare(pages, s, text_floor)
            detailed.append(m)
            # A perfect match cannot be beaten; skip the remaining comparisons.
            if m.strength >= 0.98 and not _n_changes(m) and not m.unmatched_pages:
                break
        detailed.sort(key=lambda m: (m.strength, -_n_changes(m), m.primary.published or "" if m.primary else ""),
                      reverse=True)
        return detailed

    def _prepare(self, data: bytes, kind: str) -> tuple[list[QueryPage], int, dict]:
        signals: dict = {}
        if kind == "pdf":
            info = pdf_info(data)
            n_pages = info.n_pages
            signals = pdf_signals(data, info.metadata)
            raw = iter_pages(data, kind=kind, render_width=DIFF_WIDTH, max_pages=MAX_QUERY_PAGES,
                             max_render_pages=MAX_VISUAL_PAGES, ocr=self.ocr)
        else:
            n_pages = 1
            raw = iter_pages(data, kind=kind, render_width=None, ocr=self.ocr, photo=True)
        pages = []
        self._prep_notes = []
        for p in raw:
            if p.prep.get("cropped"):
                self._prep_notes.append("The page was found inside a larger picture (screenshot bars or a "
                                        "background) and cropped to the document before comparing.")
            if p.prep.get("deskewed"):
                self._prep_notes.append(f"The picture was tilted by about {abs(p.prep['deskewed']):.1f} degrees "
                                        "and was straightened before comparing.")
            pages.append(self._query_page(p.number, p.text, p.text_source, p.image))
        return pages, n_pages, signals

    @staticmethod
    def _query_page(number: int, text: str, text_source: str, image: Image.Image | None) -> QueryPage:
        hashes = shingle_hashes(words(text)) if text else np.empty(0, dtype=np.uint64)
        ph = ink = None
        if image is not None:
            small = image.resize((HASH_WIDTH, max(1, round(image.height * HASH_WIDTH / image.width))),
                                 Image.Resampling.LANCZOS)
            ph, ink = phash(small), ink_ratio(small)
        return QueryPage(number, text, text_source, image, set(hashes.tolist()),
                         minhash(hashes) if hashes.size else None, ph, ink)

    def _candidates(self, pages: list[QueryPage], extra: frozenset[str] = frozenset()) -> dict[str, float]:
        idx = self.index
        scores: dict[str, float] = {}
        hidden = np.zeros(len(idx), dtype=bool)
        for sha in extra:
            rows = idx.rows_by_sha.get(sha)
            if rows is not None:
                hidden[rows] = True

        def bump(sha: str, s: float) -> None:
            if sha not in extra and s > scores.get(sha, -1):
                scores[sha] = s

        if len(idx):
            for p in pages:
                if p.signature is not None and len(p.shingles) >= MIN_QUERY_SHINGLES:
                    sim = signature_similarity(p.signature, idx.sigs)
                    sim[~idx.text_ok | hidden] = 0
                    for i in np.argsort(-sim)[:20]:
                        if sim[i] >= 0.03:
                            bump(idx.sha[i], float(sim[i]))
                if p.phash is not None and (p.ink or 0) >= MIN_INK:
                    d = hamming_many(p.phash, idx.phash)
                    d[~idx.visual_ok | hidden] = 99
                    for i in np.argsort(d)[:10]:
                        if d[i] <= VISUAL_MAX_HAMMING:
                            bump(idx.sha[i], 1 - float(d[i]) / 64)
        for sha in self._fts_candidates(pages):
            bump(sha, 0.05)
        return scores

    def _fts_candidates(self, pages: list[QueryPage], limit: int = 30) -> list[str]:
        vocab = {w for p in pages for w in words(p.text) if len(w) >= 5 and not w.isdigit()}
        if not vocab:
            return []
        terms = sorted(vocab, key=lambda w: (-len(w), w))[:30]
        q = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.db.conn.execute(
                "SELECT p.sha256 FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
                "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?", (q, limit)).fetchall()
        except Exception:  # noqa: BLE001 - malformed FTS input; retrieval still has two other routes
            return []
        seen: list[str] = []
        for (sha,) in rows:
            if sha not in seen and sha not in self.exclude:
                seen.append(sha)
        return seen

    def _rescore(self, pages: list[QueryPage], prelim: dict[str, float]) -> list[str]:
        """Exact 3-gram containment and best page-hash distance for the top preliminary candidates."""
        top = sorted(prelim, key=prelim.get, reverse=True)[:MAX_RESCORE]
        total = sum(len(p.shingles) for p in pages if len(p.shingles) >= MIN_QUERY_SHINGLES)
        keyed = []
        for sha in top:
            off = {no: set(shingle_hashes(words(t)).tolist()) for no, t in self.db.page_texts(sha) if t}
            hit = 0
            for p in pages:
                if len(p.shingles) >= MIN_QUERY_SHINGLES and off:
                    hit += max(len(p.shingles & s) for s in off.values())
            containment = hit / total if total else 0.0
            visual = 0.0
            rows = self.index.rows_by_sha.get(sha)
            if rows is not None:
                for p in pages:
                    if p.phash is not None and (p.ink or 0) >= MIN_INK:
                        ok = rows[self.index.visual_ok[rows]]
                        if ok.size:
                            d = int(hamming_many(p.phash, self.index.phash[ok]).min())
                            if d <= VISUAL_MAX_HAMMING:
                                visual = max(visual, 1 - d / 64)
            keyed.append((max(containment, visual), sha))
        keyed.sort(reverse=True)
        return [sha for score, sha in keyed if score > 0]

    def _compare(self, pages: list[QueryPage], sha: str, text_floor: float | None = None) -> Match:
        row = self.db.file(sha)
        match = Match(sha, self.db.provenance(sha), row["n_pages"] if row else None)
        texts = dict(self.db.page_texts(sha))
        off_sh = {no: set(shingle_hashes(words(t)).tolist()) for no, t in texts.items() if t}
        rows = self.index.rows_by_sha.get(sha, np.empty(0, dtype=int))

        hit = total = 0
        for p in pages:
            mapped = None
            if len(p.shingles) >= MIN_QUERY_SHINGLES and off_sh:
                no, s = max(off_sh.items(), key=lambda kv: len(p.shingles & kv[1]))
                c = len(p.shingles & s)
                hit += c
                total += len(p.shingles)
                if c / len(p.shingles) >= PAGE_TEXT_MATCH:
                    mapped = no
            if mapped is None and p.phash is not None and (p.ink or 0) >= MIN_INK and rows.size:
                ok = rows[self.index.visual_ok[rows]]
                if ok.size:
                    d = hamming_many(p.phash, self.index.phash[ok])
                    j = int(np.argmin(d))
                    if d[j] <= VISUAL_MAX_HAMMING:
                        mapped = int(self.index.page_no[ok][j])
            if mapped is None:
                if p.text.strip() or (p.ink or 0) >= MIN_INK:
                    match.unmatched_pages.append(p.number)
            else:
                match.page_map[p.number] = mapped
        match.text_containment = hit / total if total else None

        submitted_text = "\n".join(p.text for p in pages if p.text)
        text_mapped = [match.page_map[p.number] for p in pages if p.number in match.page_map and p.text]
        if submitted_text and text_mapped:
            lo, hi = min(text_mapped), max(text_mapped)
            official_text = "\n".join(texts.get(n, "") for n in range(lo, hi + 1))
            match.text = compare_text(official_text, submitted_text)

        # If the submission's own text says this is a different document, the
        # page images cannot overrule it (see _decide), so skip rendering them.
        if text_floor is not None and match.text_containment is not None                 and match.text_containment < text_floor:
            return match

        data = self.store.get(sha) if self.store.has(sha) else None
        if data is not None:
            kind = row["kind"] if row else "pdf"
            pairs = [(p, match.page_map[p.number]) for p in pages
                     if p.image is not None and p.number in match.page_map][:MAX_VISUAL_PAGES]
            for p, no in pairs:
                try:
                    official_img = (render_pdf_page(data, no, DIFF_WIDTH) if kind == "pdf"
                                    else load_image(data, DIFF_WIDTH))
                except Exception:  # noqa: BLE001 - a broken official page just loses its visual check
                    continue
                match.visual.append(compare_images(official_img, p.image, query_page=p.number,
                                                   official_page=no))
            if kind == "pdf":
                try:
                    match.official_signals = pdf_signals(data, pdf_info(data).metadata)
                except UnsupportedFile:
                    pass
        return match

    # -- verdict --------------------------------------------------------------

    def _decide(self, res: VerificationResult, pages: list[QueryPage], best: Match | None) -> None:
        info = res.index
        n_files = info.get("files_indexed", 0)
        text_layer = res.text_source == "layer"
        has_text_match = best is not None and (best.text_containment or 0) >= TEXT_MATCH
        same_pages = [v for v in best.visual if v.same_document] if best else []
        # Pages on the same letterhead can look alike. When the submission has
        # readable text and that text disagrees, the picture does not get a say.
        if best is not None and best.text_containment is not None:
            floor = PAGE_TEXT_MATCH if res.text_source == "layer" else 0.15
            if best.text_containment < floor:
                same_pages = []

        if best is None or not (has_text_match or same_pages):
            res.verdict = NO_MATCH
            res.summary = (f"Nothing in the index of {n_files:,} official documents matches this. "
                           "Treat it as unverified.")
            if res.text_source == "none" and not any((p.ink or 0) >= MIN_INK for p in pages):
                res.notes.append("The file has no readable text and the pages look blank.")
            elif res.text_source == "none" and self.ocr is None:
                res.notes.append("The file has no text layer and OCR is not installed, so it could only be "
                                 "compared by page image. A photo of a printout will not match that way; a "
                                 "screenshot or the original PDF will.")
            if best is not None and (best.text_containment or 0) >= 0.15 and best.primary:
                res.notes.append(f"It shares some wording ({best.text_containment:.0%}) with “{best.primary.title}”"
                                 f"{_dated(best.primary.published)}, which usually means a reused letterhead or "
                                 "boilerplate rather than the same document.")
            res.notes.append(_freshness(info))
            res.match = None
            return

        res.match = best
        text_changes = best.text.changes if best.text else []
        changed_pages = [v for v in same_pages if v.boxes]
        p = best.primary
        title = f"“{p.title}”{_dated(p.published)}" if p else best.sha256[:12]

        if text_layer and has_text_match:
            localized = [v for v in changed_pages if v.changed_ratio <= LOCALIZED_CHANGE]
            if text_changes or best.unmatched_pages:
                res.verdict = ALTERED
            elif localized:
                res.verdict = ALTERED
                res.notes.append("The text is identical to the official file, but part of the page looks "
                                 "different. Something may have been pasted over the original text.")
            else:
                res.verdict = VERIFIED_CONTENT
                if changed_pages:
                    res.notes.append("The page renders slightly differently from the official file (fonts or "
                                     "print settings); the text is identical.")
        elif same_pages:
            res.verdict = ALTERED if (changed_pages or best.unmatched_pages) else VERIFIED_CONTENT
            if res.text_source == "ocr" and text_changes:
                res.notes.append("Text differences below come from OCR and may include recognition errors; "
                                 "the verdict rests on the page image comparison.")
        else:
            high = [c for c in text_changes if c.severity == "high"]
            res.verdict = ALTERED if (high or best.unmatched_pages) else VERIFIED_CONTENT
            res.notes.append("Compared by OCR text only (no page image lined up), so small differences may be "
                             "recognition errors.")

        n_diff = len(text_changes) + sum(len(v.boxes) for v in changed_pages if not text_changes)
        if res.verdict == VERIFIED_CONTENT:
            res.summary = (f"Same content as the official {title}. The file itself is a different copy "
                           "(re-saved, printed or screenshotted).")
        else:
            parts = []
            if text_changes:
                parts.append(f"{len(text_changes)} text change{'s' * (len(text_changes) != 1)}")
            if changed_pages:
                nb = sum(len(v.boxes) for v in changed_pages)
                parts.append(f"{nb} changed region{'s' * (nb != 1)} on the page image")
            if best.unmatched_pages:
                parts.append(f"page(s) {', '.join(map(str, best.unmatched_pages))} not in the official file")
            res.summary = f"Closely matches the official {title}, but differs: {'; '.join(parts) or n_diff}."
            if text_changes and all(c.kind == "replace" and c.reasons and set(c.reasons) <= {"date", "number"}
                                    for c in text_changes):
                res.notes.append("Only dates or numbers differ. Some letters are reissued each year with new dates, "
                                 "so if this one is recent, refresh the index (`verify --refresh`) before "
                                 "concluding it was edited.")
            res.notes.append(_freshness(info))

        if best.n_pages and best.page_map:
            covered = sorted(set(best.page_map.values()))
            if len(covered) < best.n_pages:
                res.notes.append(f"The submission covers page(s) {_ranges(covered)} of the official "
                                 f"{best.n_pages}-page file.")
        res.notes += _provenance_notes(best.provenance)
        if res.kind == "pdf":
            res.notes += compare_signals(res.signals, best.official_signals)

    def _index_info(self) -> dict:
        return self.db.summary()


_SUBJECT = re.compile(r"(?im)^\s*(?:subject|sub)\s*[:.\-]+\s*(.{8,})$")


def _subject_line(pages: list[QueryPage]) -> str:
    for p in pages[:2]:
        m = _SUBJECT.search(p.text or "")
        if m:
            return " ".join(m.group(1).split())[:90]
    return ""


def _confirmed(m: Match | None, text_source: str | None) -> bool:
    """Whether a candidate is backed by matching text or a lined-up page image."""
    if m is None:
        return False
    return (m.text_containment or 0) >= TEXT_MATCH or any(v.same_document for v in m.visual)


def _n_changes(m: Match) -> int:
    return (len(m.text.changes) if m.text else 0) + sum(len(v.boxes) for v in m.visual)


def _dated(published: str | None) -> str:
    return f" ({published})" if published else ""


def _freshness(info: dict) -> str:
    when = (info.get("last_listing_sync") or "never")[:10]
    return f"Index last synced with the issuer's site on {when}."


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def _provenance_notes(prov: list[Provenance]) -> list[str]:
    notes = []
    if prov and all(not p.is_current for p in prov):
        notes.append("The issuer has since replaced or removed this file at its original address.")
    if prov and all(p.origin == "wayback" for p in prov):
        notes.append("The official copy is a Wayback Machine capture of the issuer's URL; the live link is dead.")
    if len({p.title for p in prov}) > 1:
        notes.append(f"The same file is linked from {len(prov)} listing entries.")
    return notes
