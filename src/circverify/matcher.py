"""Find the official document a submission came from, and say how it differs.

Pipeline, cheapest and strongest evidence first:

1. SHA-256 lookup against official files (and against known debunked fakes).
2. Embedded digital signature, if the PDF carries one.
3. Candidate retrieval: MinHash of each page's word 3-grams, a full-text
   query, the perceptual hash of the rendered page, and the reference number
   printed on the letter.
4. Exact re-scoring: share of the submission's 3-grams found in each candidate.
5. Detailed comparison of the best few: word diff and aligned pixel diff.
6. Verdict. Nothing here learns what a forgery looks like; the references are
   what the issuer published and what the issuer or a fact-checker debunked.

Verdicts: Verified (exact copy or same content), Altered, Known fake, Not
found, Unreadable. "Not found" never means fake: it means unverified.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

import numpy as np
from PIL import Image

from .config import issuer_date
from .db import Database, Provenance
from .diff import DIFF_WIDTH, TextComparison, VisualDiff, compare_images, compare_text
from .extract import HASH_WIDTH, UnsupportedFile, iter_pages, load_image, pdf_info, render_pdf_page, sniff_kind
from .fields import extract_fields
from .fingerprint import (
    NUM_PERM,
    hamming_many,
    header_fingerprint,
    ink_ratio,
    minhash,
    phash,
    shingle_hashes,
    signature_similarity,
    to_unsigned64,
    words,
)
from .forensics import compare_signals, image_notes, image_signals, pdf_signals
from .layout import LayoutModel, layout_features
from .layout import describe as describe_layout
from .links import check as check_links
from .links import describe as describe_link
from .links import text_domains
from .imageprep import flatten_lighting
from .ocr import load_ocr
from .signatures import describe as describe_signature
from .signatures import pdf_signatures
from .store import BlobStore

VERIFIED_EXACT = "VERIFIED_EXACT"
VERIFIED_CONTENT = "VERIFIED_CONTENT"
# The page is an official one, but only a photo was compared: a small edit (a changed digit) cannot be ruled out.
UNCONFIRMED_MATCH = "UNCONFIRMED_MATCH"
ALTERED = "ALTERED"
KNOWN_FAKE = "KNOWN_FAKE"
NO_MATCH = "NO_MATCH"
UNREADABLE = "UNREADABLE"

MAX_QUERY_PAGES = 30
MAX_VISUAL_PAGES = 6
MAX_RESCORE = 15
MAX_DETAILED = 3
# Without text, candidates are ranked by page hash alone, and letters on one template hash alike:
# compare more of them in full before giving up.
MAX_DETAILED_IMAGE_ONLY = 8
MIN_QUERY_SHINGLES = 8
TEXT_MATCH = 0.5  # share of the submission's word 3-grams found in the official text
PAGE_TEXT_MATCH = 0.3
VISUAL_MAX_HAMMING = 14
MIN_INK = 0.003
LOCALIZED_CHANGE = 0.10
ROTATION_GATE = 10  # pHash distance a rotated picture must reach to be compared in full
LETTERHEAD_CLOSE = 8  # header pHash distance counted as "the same letterhead"


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
    # Hash of the picture exactly as submitted, kept when cleaning it up (crop, deskew,
    # rectify) changed it: if the clean-up misjudged the page, retrieval still finds it.
    alt_phash: int | None = None
    # Photographed and lighting-corrected: the official page gets the same correction before
    # comparing, so large grey artwork (a footer graphic, a tinted banner) is treated alike.
    photographed: bool = False

    def distances(self, hashes: np.ndarray) -> np.ndarray:
        d = hamming_many(self.phash, hashes)
        if self.alt_phash is not None:
            d = np.minimum(d, hamming_many(self.alt_phash, hashes))
        return d


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
    official_fields: dict | None = None

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
            "official_fields": self.official_fields,
            "official_signals": self.official_signals,
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
    signal_notes: list[str] = field(default_factory=list)  # forensic hints, shown only for "not found"
    index: dict = field(default_factory=dict)
    search_hint: str = ""  # the submission's subject line, to prefill a search for the real notice
    fields: dict = field(default_factory=dict)  # reference number, date, subject, signatory of the submission
    signatures: list[dict] = field(default_factory=list)
    known_fake: dict | None = None  # the debunk this submission matches
    withdrawn: str | None = None  # date the issuer stopped listing the official document
    # The official file is no longer listed anywhere and is known from the Wayback Machine:
    # the date of its first archived capture ("" if unknown). None for listed documents.
    archived_only: str | None = None
    links: list[dict] = field(default_factory=list)  # links, addresses and QR codes, with their domains' groups
    layout: dict | None = None  # how unusual the first page's layout is next to the issuer's own
    evidence: list[dict] = field(default_factory=list)  # the checklist behind the verdict (evidence.py)
    updated_by: list[dict] = field(default_factory=list)  # later documents that may amend or replace the match
    updates: list[dict] = field(default_factory=list)  # earlier documents the match itself amends or replaces
    key_dates: list[dict] = field(default_factory=list)  # deadlines and event dates in the official text
    ocr_confidence: float | None = None  # mean OCR confidence when the submission's words were read by OCR
    duplicates: list[dict] = field(default_factory=list)  # other official files with the same text

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "file": {"name": self.filename, "sha256": self.sha256, "kind": self.kind,
                     "pages": self.n_pages, "text_source": self.text_source},
            "match": self.match.to_dict() if self.match else None,
            "known_fake": self.known_fake,
            "withdrawn": self.withdrawn,
            "archived_only": self.archived_only,
            "links": self.links,
            "layout": self.layout,
            "evidence": self.evidence,
            "updated_by": self.updated_by,
            "updates": self.updates,
            "key_dates": self.key_dates,
            "ocr_confidence": self.ocr_confidence,
            "duplicates": self.duplicates,
            "signatures": self.signatures,
            "fields": {k: v for k, v in self.fields.items() if k != "ref_key"},
            "alternatives": self.alternatives,
            "notes": self.notes,
            "signal_notes": self.signal_notes,
            "signals": self.signals,
            "index": self.index,
        }


class SignatureIndex:
    """Every indexed page's fingerprints, held in arrays for vectorised comparison."""

    def __init__(self, db: Database, exclude: frozenset[str] = frozenset(), *, rows=None):
        if rows is None:
            rows = db.conn.execute(
                "SELECT sha256, page_no, n_shingles, minhash, phash, ink FROM pages ORDER BY sha256, page_no")
        rows = [r for r in rows if r[0] not in exclude]
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
        groups: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            groups[r[0]].append(i)
        self.rows_by_sha = {k: np.array(v) for k, v in groups.items()}

    def __len__(self) -> int:
        return len(self.sha)


class Verifier:
    def __init__(self, db: Database, store: BlobStore, *, use_ocr: bool | None = None,
                 exclude: frozenset[str] | set[str] = frozenset(), ocr=None, known_fakes: bool = True):
        self.db = db
        self.store = store
        self.exclude = frozenset(exclude)
        self._use_ocr = use_ocr
        self._ocr = ocr
        self._ocr_loaded = ocr is not None
        self.index = SignatureIndex(db, self.exclude)
        self._fakes: list = []
        self._fake_texts: dict[str, list[tuple[int, str]]] = {}
        if known_fakes:
            self._load_fakes()
        header_rows = db.header_hashes()
        self._headers = np.array([to_unsigned64(r["header_phash"]) for r in header_rows], dtype=np.uint64)
        self._prep_notes: list[str] = []
        self._excerpt = False
        self._profile: dict | None = None

    @property
    def profile(self) -> dict:
        """What the issuer's own documents look like: domains they mention, tools that wrote them, layouts.
        Loaded on first use, because only unverified submissions need it."""
        if self._profile is None:
            producers, total = self.db.producer_counts()
            self._profile = {"domains": self.db.known_domains(), "producers": producers, "pdfs": total,
                             "layout": LayoutModel.from_rows(self.db.layout_rows())}
        return self._profile

    @property
    def ocr(self):
        """Loaded on first use: OCR is a fallback, and loading an engine is not free."""
        if not self._ocr_loaded:
            self._ocr = load_ocr(self._use_ocr)
            self._ocr_loaded = True
        return self._ocr

    def _load_fakes(self) -> None:
        rows = self.db.fake_pages()
        self._fakes = rows
        texts: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for r in rows:
            texts[r["sha256"]].append((r["page_no"], r["text"]))
        self._fake_texts = dict(texts)

    # -- public ---------------------------------------------------------------

    def verify(self, data: bytes, filename: str | None = None, *,
               exclude: set[str] | frozenset[str] | None = None) -> VerificationResult:
        """Check one document. ``exclude`` hides files from this call only (used for hold-out tests)."""
        return self._finish(self._verify(data, filename, exclude=exclude))

    def verify_text(self, text: str, label: str = "Pasted text", *,
                    exclude: set[str] | frozenset[str] | None = None) -> VerificationResult:
        """Check text copied from a message, for when there is no file to upload."""
        return self._finish(self._verify_text(text, label, exclude=exclude))

    def _same_text(self, sha: str, limit: int = 4) -> list[dict]:
        """Other official files whose first page has the same text: re-posts, copies under another entry."""
        idx = self.index
        rows = idx.rows_by_sha.get(sha)
        if rows is None:
            return []
        first = rows[idx.page_no[rows] == 1]
        if not first.size or not idx.text_ok[first[0]] or idx.n_shingles[first[0]] < 40:
            return []
        sim = signature_similarity(idx.sigs[first[0]], idx.sigs)
        sim[~idx.text_ok | (idx.page_no != 1)] = 0
        out, seen = [], {sha}
        for i in np.argsort(-sim)[:20]:
            other = idx.sha[i]
            if sim[i] < 0.9 or other in seen:
                continue
            seen.add(other)
            prov = self.db.provenance(other)
            if prov:
                p = min(prov, key=lambda x: x.published or "9999")
                out.append({"sha256": other, "title": p.title, "published": p.published, "url": p.url,
                            "similarity": round(float(sim[i]), 3)})
            if len(out) == limit:
                break
        return out

    def _finish(self, res: VerificationResult) -> VerificationResult:
        from .evidence import summarize

        res.updated_by = self.db.later_documents(res.match.sha256) if res.match else []
        res.updates = self.db.earlier_documents(res.match.sha256) if res.match else []
        if res.match is not None:
            from .deadlines import key_dates

            official = self.db.fields(res.match.sha256)
            res.key_dates = key_dates(self.db.first_pages_text(res.match.sha256, 4),
                                      official["doc_date"] if official else None)
            res.duplicates = self._same_text(res.match.sha256)
            if official is not None:
                res.match.official_fields = {k: official[k] for k in ("ref_no", "doc_date", "subject", "signatory")}
        res.evidence = summarize(res, self.db)
        return res

    def _verify(self, data: bytes, filename: str | None = None, *,
                exclude: set[str] | frozenset[str] | None = None) -> VerificationResult:
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
                self._apply_provenance(res, prov)
                if kind == "pdf":
                    self._check_signatures(res, data)
                return res
            debunks = self.db.debunks_for(sha) if self._fakes else []
            if debunks:
                res.verdict = KNOWN_FAKE
                res.known_fake = _debunk_dict(debunks, sha, 1.0, exact=True)
                res.summary = _fake_summary(res.known_fake)
                return res

        try:
            pages, n_pages, signals = self._prepare(data, kind)
        except UnsupportedFile as exc:
            res.summary = f"Could not read the file: {exc}"
            return res
        res.n_pages = n_pages
        res.signals = signals
        if kind == "pdf":
            self._check_signatures(res, data)
        return self._evaluate(res, pages, excluded - self.exclude, photo=kind != "pdf", ocr_allowed=True)

    def _verify_text(self, text: str, label: str = "Pasted text", *,
                     exclude: set[str] | frozenset[str] | None = None) -> VerificationResult:
        text = (text or "").strip()
        res = VerificationResult(verdict=UNREADABLE, summary="", sha256=hashlib.sha256(text.encode()).hexdigest(),
                                 filename=label, kind="text", n_pages=1, index=self._index_info())
        page = self._query_page(1, text, "layer", None)
        if len(page.shingles) < MIN_QUERY_SHINGLES:
            res.summary = "Paste at least two or three full sentences of the circular so it can be matched."
            return res
        self._prep_notes = []
        self._excerpt = True
        try:
            return self._evaluate(res, [page], frozenset(exclude or ()) - self.exclude, photo=False,
                                  ocr_allowed=False)
        finally:
            self._excerpt = False

    # -- the shared path ------------------------------------------------------

    def _evaluate(self, res: VerificationResult, pages: list[QueryPage], extra: frozenset[str], *,
                  photo: bool, ocr_allowed: bool) -> VerificationResult:
        sources = {p.text_source for p in pages}
        res.text_source = "layer" if "layer" in sources else ("ocr" if "ocr" in sources else "none")
        res.search_hint = _subject_line(pages)
        res.fields = extract_fields("\n".join(p.text for p in pages[:2]))
        text_floor = {"layer": PAGE_TEXT_MATCH, "ocr": 0.15}.get(res.text_source)

        detailed = self._match(pages, extra, text_floor, res.fields.get("ref_key"))
        best = detailed[0] if detailed else None
        if photo and pages and pages[0].image is not None and not _confirmed(best):
            pages, detailed, best = self._try_rotations(res, pages, extra, text_floor, detailed, best)
        if ocr_allowed and not _confirmed(best) and res.text_source == "none" and self.ocr is not None:
            pages, detailed, best = self._try_ocr(res, pages, extra, detailed, best)
        elif ocr_allowed and res.text_source == "none" and self.ocr is not None and _photo_only(best, pages):
            pages, best = self._confirm_with_ocr(res, pages, best)
        res.notes.extend(self._prep_notes)
        res.alternatives = [
            {"sha256": m.sha256, "title": m.primary.title if m.primary else None,
             "published": m.primary.published if m.primary else None,
             "url": m.primary.url if m.primary else None, "strength": round(m.strength, 3)}
            for m in detailed[1:] if m.strength >= TEXT_MATCH
        ]
        self._decide(res, pages, best)
        if res.verdict not in (VERIFIED_EXACT, VERIFIED_CONTENT):
            fake = self._match_fakes(pages)
            if fake:
                res.known_fake = fake
                res.verdict = KNOWN_FAKE
                res.summary = _fake_summary(fake)
                if res.match:
                    res.notes.insert(0, "It is also close to a real notice, shown below with what was changed: "
                                        "the fake appears to be built from it.")
        if res.verdict not in (VERIFIED_EXACT, VERIFIED_CONTENT):
            self._link_notes(res, pages)
        if res.verdict == NO_MATCH:
            self._not_found_notes(res, pages)
        return res

    def _link_notes(self, res: VerificationResult, pages: list[QueryPage]) -> None:
        """Links, addresses and QR codes, and whether their domains are the issuer's. Hints only."""
        text = "\n".join(p.text for p in pages if p.text)
        images = [(p.number, p.image) for p in pages if p.image is not None][:3]
        found = check_links(text, images, self.profile["domains"])
        res.links = [f.to_dict() for f in found]
        official_domains: set[str] = set()
        if res.verdict == ALTERED and res.match is not None:
            # Only what this copy added: links the official file itself carries are not news.
            official_domains = text_domains("\n".join(t for _, t in self.db.page_texts(res.match.sha256) if t))
        worth = ("lookalike", "shortener", "unknown", "not-a-link") if res.verdict != ALTERED else ("lookalike", "shortener")
        notes = []
        for f in found:
            if f.kind in worth and f.domain not in official_domains:
                s = describe_link(f)
                if s and s not in notes:
                    notes.append(s)
        res.signal_notes.extend(notes[:4])

    def _try_rotations(self, res, pages, extra, text_floor, detailed, best):
        # A sideways or upside-down picture always finds some lookalike on the shared
        # letterhead, so retry the other orientations whenever nothing is confirmed.
        upright = self._closest_page_hash(pages)
        for angle in (90, 270, 180):
            turned = [self._query_page(p.number, p.text, p.text_source, p.image.rotate(angle, expand=True))
                      for p in pages]
            # Cheap gate: only run the full comparison if this orientation looks clearly closer.
            if self._closest_page_hash(turned) > min(ROTATION_GATE, upright - 4):
                continue
            tried = self._match(turned, extra, text_floor, None)
            if tried and _confirmed(tried[0]):
                res.notes.append(f"The picture was rotated by {angle} degrees and was turned upright to compare.")
                return turned, tried, tried[0]
        return pages, detailed, best

    def _try_ocr(self, res, pages, extra, detailed, best):
        """Read the words when the picture alone could not be matched (crops, photos, retyped copies)."""
        read = []
        for p in pages[:MAX_VISUAL_PAGES]:
            text = ""
            if p.image is not None:
                try:
                    text = self.ocr(p.image) or ""
                except Exception:  # noqa: BLE001 - a failed OCR run just means no text
                    text = ""
            read.append(self._query_page(p.number, text, "ocr" if text.strip() else "none", p.image))
        if not any(q.text.strip() for q in read):
            return pages, detailed, best
        res.text_source = "ocr"
        res.ocr_confidence = _mean_confidence(read)
        res.fields = extract_fields("\n".join(q.text for q in read[:2]))
        res.search_hint = res.search_hint or _subject_line(read)
        tried = self._match(read, extra, 0.15, res.fields.get("ref_key"))
        if tried and (not best or tried[0].strength > best.strength):
            res.notes.append("The words were read with OCR because the picture alone could not be matched.")
            return read, tried, tried[0]
        return read, detailed, best

    def _confirm_with_ocr(self, res, pages, best):
        """A photo matched an official page by image alone: read its words to check the details too."""
        read = []
        for p in pages[:MAX_VISUAL_PAGES]:
            text = ""
            if p.image is not None:
                try:
                    text = self.ocr(p.image) or ""
                except Exception:  # noqa: BLE001 - a failed OCR run just means no text
                    text = ""
            q = self._query_page(p.number, text, "ocr" if text.strip() else "none", p.image)
            q.photographed, q.alt_phash = p.photographed, p.alt_phash
            read.append(q)
        if not any(q.text.strip() for q in read):
            return pages, best
        res.text_source = "ocr"
        res.ocr_confidence = _mean_confidence(read)
        res.fields = extract_fields("\n".join(q.text for q in read[:2]))
        checked = self._compare(read, best.sha256)
        res.notes.append("The photo matched by its image; its words were then read with OCR to check the details.")
        return read, checked

    # -- stages ---------------------------------------------------------------

    def _closest_page_hash(self, pages: list[QueryPage]) -> int:
        idx = self.index
        best = 64
        for p in pages:
            if p.phash is not None and (p.ink or 0) >= MIN_INK and len(idx):
                d = p.distances(idx.phash)
                d[~idx.visual_ok] = 99
                best = min(best, int(d.min()))
        return best

    def _match(self, pages: list[QueryPage], extra: frozenset[str], text_floor: float | None,
               ref_key: str | None) -> list[Match]:
        prelim = self._candidates(pages, extra)
        if ref_key:
            for sha in self.db.files_with_ref(ref_key):
                if sha not in extra and sha not in self.exclude:
                    prelim[sha] = max(prelim.get(sha, 0), 0.9)
        candidates = self._rescore(pages, prelim)
        image_only = not any(len(p.shingles) >= MIN_QUERY_SHINGLES for p in pages)
        detailed = []
        for s in candidates[:MAX_DETAILED_IMAGE_ONLY if image_only else MAX_DETAILED]:
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
                             max_render_pages=MAX_VISUAL_PAGES)
        else:
            n_pages = 1
            signals = image_signals(data)
            raw = iter_pages(data, kind=kind, render_width=None, photo=True)
        pages = []
        self._prep_notes = []
        for p in raw:
            if p.prep.get("rectified"):
                self._prep_notes.append("The page was found inside the picture, flattened from the angle it was "
                                        "photographed at, and its lighting evened out before comparing.")
            if p.prep.get("cropped"):
                self._prep_notes.append("The page was found inside a larger picture (screenshot bars or a "
                                        "background) and cropped to the document before comparing.")
            if p.prep.get("deskewed"):
                self._prep_notes.append(f"The picture was tilted by about {abs(p.prep['deskewed']):.1f} degrees "
                                        "and was straightened before comparing.")
            q = self._query_page(p.number, p.text, p.text_source, p.image)
            q.photographed = bool(p.prep.get("rectified"))
            if kind != "pdf" and q.phash is not None and any(p.prep.get(k) for k in ("rectified", "cropped", "deskewed")):
                q.alt_phash = self._as_submitted_hash(data)
            pages.append(q)
        return pages, n_pages, signals

    @staticmethod
    def _as_submitted_hash(data: bytes) -> int | None:
        import io

        from PIL import ImageOps

        from .imageprep import open_checked, to_gray

        try:
            img = to_gray(ImageOps.exif_transpose(open_checked(Image.open(io.BytesIO(data)))))
        except Exception:  # noqa: BLE001 - the prepared hash is still there
            return None
        small = img.resize((HASH_WIDTH, max(1, round(img.height * HASH_WIDTH / img.width))), Image.Resampling.LANCZOS)
        return phash(small)

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
                    d = p.distances(idx.phash)
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
        except Exception:  # noqa: BLE001 - malformed FTS input; retrieval still has other routes
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
                            d = int(p.distances(self.index.phash[ok]).min())
                            if d <= VISUAL_MAX_HAMMING:
                                visual = max(visual, 1 - d / 64)
            keyed.append((max(containment, visual, 0.01 if prelim.get(sha, 0) >= 0.9 else 0), sha))
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
                    d = p.distances(self.index.phash[ok])
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
            match.text = compare_text(official_text, submitted_text, excerpt=self._excerpt)

        # If the submission's own text says this is a different document, the
        # page images cannot overrule it (see _decide), so skip rendering them.
        if text_floor is not None and match.text_containment is not None and match.text_containment < text_floor:
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
                at = (lambda w, _d=data, _n=no: render_pdf_page(_d, _n, w)) if kind == "pdf" else None
                if p.photographed:
                    official_img = flatten_lighting(official_img.convert("L"))
                    at = None
                match.visual.append(compare_images(official_img, p.image, query_page=p.number,
                                                   official_page=no, official_at=at))
            if kind == "pdf":
                try:
                    match.official_signals = pdf_signals(data, pdf_info(data).metadata)
                except UnsupportedFile:
                    pass
        return match

    # -- known fakes ----------------------------------------------------------

    def _match_fakes(self, pages: list[QueryPage]) -> dict | None:
        """Match against fakes the issuer or a fact-checker has publicly debunked."""
        if not self._fakes:
            return None
        best: tuple[float, str] | None = None
        by_sha: dict[str, list] = defaultdict(list)
        for r in self._fakes:
            by_sha[r["sha256"]].append(r)
        total = sum(len(p.shingles) for p in pages if len(p.shingles) >= MIN_QUERY_SHINGLES)
        # Known fakes are few, and a forwarded copy is often cropped differently from the
        # debunk image, so always compare the closest few in full rather than gate on the hash.
        dist = {}
        for sha, rows in by_sha.items():
            ds = [int(p.distances(np.array([to_unsigned64(r["phash"])], dtype=np.uint64))[0])
                  for r in rows if r["phash"] is not None for p in pages if p.phash is not None]
            if ds:
                dist[sha] = min(ds)
        visual_shortlist = set(sorted(dist, key=dist.get)[:3]) | {s for s, d in dist.items() if d <= VISUAL_MAX_HAMMING}
        for sha, rows in by_sha.items():
            strength = 0.0
            if total:
                fake_sh = [set(shingle_hashes(words(t)).tolist()) for _, t in self._fake_texts.get(sha, []) if t]
                if fake_sh:
                    hit = sum(max(len(p.shingles & s) for s in fake_sh) for p in pages
                              if len(p.shingles) >= MIN_QUERY_SHINGLES)
                    containment = hit / total
                    if containment >= TEXT_MATCH:
                        strength = containment
            if sha in visual_shortlist and self.store.has(sha):
                fake_img = self._fake_image(sha)
                if fake_img is not None:
                    for p in pages:
                        if p.image is None:
                            continue
                        v = compare_images(fake_img, p.image, keep_images=False)
                        if v.same_document:
                            strength = max(strength, 1 - v.raw_changed_ratio)
            if strength and (best is None or strength > best[0]):
                best = (strength, sha)
        if best is None:
            return None
        return _debunk_dict(self.db.debunks_for(best[1]), best[1], best[0], exact=False)

    def _fake_image(self, sha: str) -> Image.Image | None:
        try:
            data = self.store.get(sha)
            page = next(iter_pages(data, render_width=DIFF_WIDTH, photo=True, max_pages=1))
            return page.image
        except Exception:  # noqa: BLE001
            return None

    # -- extra evidence -------------------------------------------------------

    def _check_signatures(self, res: VerificationResult, data: bytes) -> None:
        res.signatures = pdf_signatures(data)
        for sig in res.signatures:
            res.notes.insert(0, describe_signature(sig))

    def _apply_provenance(self, res: VerificationResult, prov: list[Provenance]) -> None:
        removed = [p.removed_on for p in prov if p.removed_on]
        if removed and len(removed) == len(prov):
            res.withdrawn = min(removed)[:10]
        archived = [p.first_archived for p in prov if p.first_archived]
        if prov and all(p.source == "archive" for p in prov):
            res.archived_only = min(archived)[:10] if archived else ""
        if archived:
            res.notes.append(f"The Wayback Machine first captured this file on {min(archived)[:10]}, "
                             "independent evidence of when it was public.")
        res.notes += _provenance_notes(prov)

    def _not_found_notes(self, res: VerificationResult, pages: list[QueryPage]) -> None:
        """Explainable hints for an unverified document. Hints only, never a verdict."""
        best = getattr(self, "_closest", None)
        if res.text_source == "none" and not any((p.ink or 0) >= MIN_INK for p in pages):
            res.notes.append("The file has no readable text and the pages look blank.")
        elif res.text_source == "none" and self.ocr is None and res.kind != "text":
            res.notes.append("The file has no text layer and no OCR engine is set up, so it could only be "
                             "compared by page image. A cropped screenshot will only match with OCR.")
        if best is not None and (best.text_containment or 0) >= 0.15 and best.primary:
            res.notes.append(f"It shares some wording ({best.text_containment:.0%}) with “{best.primary.title}”"
                             f"{_dated(best.primary.published)}, which usually means a reused letterhead or "
                             "boilerplate rather than the same document.")
        res.notes.append(_freshness(res.index))
        ref = res.fields.get("ref_no")
        if ref:
            owners = [s for s in self.db.files_with_ref(res.fields.get("ref_key") or "") if s not in self.exclude]
            if owners:
                p = (self.db.provenance(owners[0]) or [None])[0]
                if p:
                    res.signal_notes.append(f"Its reference number ({ref}) belongs to a different official "
                                            f"document, “{p.title}”{_dated(p.published)}. A real reference number on "
                                            "different text is worth checking against the official file.")
            else:
                res.signal_notes.append(f"No document in the index carries its reference number ({ref}).")
        first = pages[0] if pages else None
        if first is not None and first.image is not None and len(self._headers):
            hp, hink = header_fingerprint(first.image)
            if hink > 0.002:
                d = hamming_many(to_unsigned64(hp), self._headers)
                close = int((d <= LETTERHEAD_CLOSE).sum())
                if close >= 3:
                    res.signal_notes.append(f"Its letterhead looks like UGC's (it resembles the letterhead on {close:,} "
                                            "official documents), but its content matches none of them: new text on a "
                                            "copied letterhead looks like this.")
                else:
                    res.signal_notes.append("Its letterhead does not resemble the letterheads on the official "
                                            "documents in the index.")
        if res.kind not in ("pdf", "text"):
            res.signal_notes += image_notes(res.signals)
        if pages and pages[0].image is not None and self.profile["layout"] is not None:
            feats = layout_features(pages[0].image)
            if feats:
                score = self.profile["layout"].score(feats)
                res.layout = {"percentile": score.percentile, "unusual": score.unusual,
                              "differences": [{"feature": f, "submitted": v, "typical": t} for f, v, t in score.differences]}
                note = describe_layout(score)
                if note:
                    res.signal_notes.append(note)
        if res.kind == "pdf":
            res.signal_notes += compare_signals(res.signals, None, self.profile["producers"], self.profile["pdfs"])
            stated = res.fields.get("doc_date")
            created = (res.signals.get("created") or "")[:10]
            if stated and created and len(created) == 10:
                if created < stated:
                    res.signal_notes.append(f"The PDF was created on {created}, before the date written on it "
                                            f"({stated}).")
                elif created[:4] > stated[:4] and int(created[:4]) - int(stated[:4]) >= 1:
                    res.signal_notes.append(f"The PDF was created on {created}, more than a year after the date "
                                            f"written on it ({stated}). Old circulars are sometimes recirculated "
                                            "with new dates.")

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
                           "That does not make it fake, but it is unverified.")
            self._closest = best
            res.match = None
            return

        res.match = best
        text_changes = best.text.changes if best.text else []
        changed_pages = [v for v in same_pages if v.boxes]
        lines_to_check = sum(len(v.lines_to_check) for v in same_pages)
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
            if res.verdict == VERIFIED_CONTENT and (_photo_only(best, pages) or lines_to_check):
                ocr_agrees = (res.text_source == "ocr" and (best.text_containment or 0) >= TEXT_MATCH
                              and not [c for c in text_changes if c.severity == "high"])
                if not ocr_agrees:
                    res.verdict = UNCONFIRMED_MATCH
            if res.text_source == "ocr" and text_changes:
                res.notes.append("Text differences below come from OCR and may include recognition errors; "
                                 "the verdict rests on the page image comparison.")
        else:
            high = [c for c in text_changes if c.severity == "high"]
            res.verdict = ALTERED if (high or best.unmatched_pages) else VERIFIED_CONTENT
            res.notes.append("Compared by OCR text only (no page image lined up), so small differences may be "
                             "recognition errors.")

        if res.verdict == UNCONFIRMED_MATCH and lines_to_check and not _photo_only(best, pages):
            res.summary = (f"The page matches the official {title}, but {_plural_lines(lines_to_check)} slightly "
                           "from the official page all along. That can be retyped text, such as a changed amount or "
                           "date, or just heavy compression. Compare the marked line with the official file before "
                           "relying on it.")
        elif res.verdict == UNCONFIRMED_MATCH:
            res.summary = (f"The page matches the official {title}, but only a photo of it could be compared, and a "
                           "photo cannot rule out a small edit such as a changed date or amount. Compare the details "
                           "with the official file before relying on it.")
            if self.ocr is None:
                res.notes.append("With an OCR engine installed, the words in the photo are read and compared too, "
                                 "which can confirm the details.")
        elif res.verdict == VERIFIED_CONTENT:
            if res.kind == "text":
                res.summary = f"The text matches the official {title} word for word."
            else:
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
            res.summary = f"Closely matches the official {title}, but differs: {'; '.join(parts) or 'see below'}."
            if text_changes and all(c.kind == "replace" and c.reasons and set(c.reasons) <= {"date", "number"}
                                    for c in text_changes):
                res.notes.append("Only dates or numbers differ. Old circulars are often recirculated with new "
                                 "dates; some letters are also genuinely reissued each year, so if this one is "
                                 "recent, refresh the index (`verify --refresh`) before concluding.")
            res.notes.append(_freshness(info))

        if best.n_pages and best.page_map:
            covered = sorted(set(best.page_map.values()))
            if len(covered) < best.n_pages:
                res.notes.append(f"The submission covers page(s) {_ranges(covered)} of the official "
                                 f"{best.n_pages}-page file.")
        self._apply_provenance(res, best.provenance)

    def _index_info(self) -> dict:
        return self.db.summary()


_SUBJECT = re.compile(r"(?im)^\s*(?:subject|sub)\s*[:.\-]+\s*(.{8,})$")


def _subject_line(pages: list[QueryPage]) -> str:
    for p in pages[:2]:
        m = _SUBJECT.search(p.text or "")
        if m:
            return " ".join(m.group(1).split())[:90]
    return ""


def _mean_confidence(pages: list[QueryPage]) -> float | None:
    confs = [c for p in pages if (c := getattr(p.text, "confidence", None)) is not None]
    return round(sum(confs) / len(confs), 3) if confs else None


def _plural_lines(n: int) -> str:
    return "one line differs" if n == 1 else f"{n} lines differ"


def _photo_only(m: Match | None, pages: list[QueryPage]) -> bool:
    """The candidate rests on a photographed page lined up by its image, with no text layer compared."""
    if m is None:
        return False
    same = [v for v in m.visual if v.same_document]
    if not same or (m.text_containment or 0) >= TEXT_MATCH and any(p.text_source == "layer" for p in pages):
        return False
    return any(p.photographed for p in pages) or all(v.method == "keypoints" for v in same)


def _confirmed(m: Match | None, text_source: str | None = None) -> bool:
    """Whether a candidate is backed by matching text or a lined-up page image."""
    if m is None:
        return False
    return (m.text_containment or 0) >= TEXT_MATCH or any(v.same_document for v in m.visual)


def _n_changes(m: Match) -> int:
    return (len(m.text.changes) if m.text else 0) + sum(len(v.boxes) for v in m.visual)


def _dated(published: str | None) -> str:
    return f" ({published})" if published else ""


def _freshness(info: dict) -> str:
    dt = issuer_date(info.get("last_listing_sync"))
    when = dt.strftime("%Y-%m-%d") if dt else "never"
    return f"Index last synced with the issuer's site on {when}."


def _debunk_dict(rows, sha: str, strength: float, *, exact: bool) -> dict:
    debunks = [{"source": r["source"], "url": r["url"], "published": r["published"], "claim": r["claim"]}
               for r in rows]
    return {"sha256": sha, "strength": round(strength, 3), "exact": exact, "debunks": debunks}


def _fake_summary(fake: dict) -> str:
    d = fake["debunks"][0] if fake["debunks"] else {}
    who = d.get("source") or "a fact-checker"
    when = f" on {d['published'][:10]}" if d.get("published") else ""
    if fake.get("exact"):
        return f"This exact file was publicly debunked as fake by {who}{when}."
    return f"This matches a notice that {who} publicly debunked as fake{when}."


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
    if prov and all(p.source == "archive" for p in prov):
        notes.append("This file is no longer linked from any UGC listing; it was recovered from the Wayback "
                     "Machine's archive of ugc.gov.in, so its title comes from the file name.")
    if len({p.title for p in prov}) > 1:
        notes.append(f"The same file is linked from {len(prov)} listing entries.")
    return notes
