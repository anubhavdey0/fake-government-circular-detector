"""Explain how a submission differs from the official document.

Two independent views:

* ``compare_text``: word-level diff of the extracted text, with each change
  rated by what it touches (numbers, dates, links, money, negations).
* ``compare_images``: align a submitted page with the official page render
  and box the regions whose pixels changed. This catches edits that leave
  the text layer alone, such as a pasted-over image of a new date.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .fingerprint import nfkc, word_spans

# -- text --------------------------------------------------------------------

_MONTH = re.compile(
    r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|"
    r"oct(ober)?|nov(ember)?|dec(ember)?)\b",
    re.I,
)
_LINK = re.compile(
    r"(https?://|www\.|\S+@\S+|\b[\w-]+\.(com|in|org|net|co|info|xyz|online|site|top|link|me|io|"
    r"gov|nic|ac|edu|ly)\b)",
    re.I,
)
_MONEY = re.compile(r"(₹|\brs\b\.?|\binr\b|\brupees?\b|\blakhs?\b|\bcrores?\b|\bfees?\b|\bpayment\b)", re.I)
_MEANING = re.compile(
    r"\b(not|no|never|without|cancell?ed|withdrawn|postponed|extended|fake|bogus|unrecogni[sz]ed|"
    r"recogni[sz]ed|approved|banned|mandatory|compulsory|optional|exempt(ed)?|invalid|valid)\b",
    re.I,
)


@dataclass
class TextChange:
    kind: str  # "replace" | "insert" | "delete"
    official: str
    submitted: str
    context_before: str
    context_after: str
    severity: str  # "high" | "normal"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TextComparison:
    ratio: float
    changes: list[TextChange]
    official_words: int
    submitted_words: int
    cosmetic: int = 0  # differences only in spacing/case/punctuation, ignored


def classify(official: str, submitted: str) -> tuple[str, list[str]]:
    both = f"{official} {submitted}"
    reasons = []
    if _MONTH.search(both):
        reasons.append("date")
    if re.search(r"\d", both):
        reasons.append("number")
    if _LINK.search(both):
        reasons.append("link or contact")
    if _MONEY.search(both):
        reasons.append("money")
    if _MEANING.search(both):
        reasons.append("meaning-changing word")
    return ("high" if reasons else "normal"), reasons


def _span(text: str, spans: list[tuple[str, int, int]], i1: int, i2: int) -> str:
    """Original text of words i1..i2, widened to whitespace so a changed URL or amount shows whole."""
    if i2 <= i1:
        return ""
    start, end = spans[i1][1], spans[i2 - 1][2]
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return re.sub(r"\s+", " ", text[start:end]).strip().rstrip(",;:")


def compare_text(official_text: str, submitted_text: str, *, context: int = 6, merge_gap: int = 2) -> TextComparison:
    a_text, b_text = nfkc(official_text), nfkc(submitted_text)
    A, B = word_spans(a_text), word_spans(b_text)
    a = [w.casefold() for w, _, _ in A]
    b = [w.casefold() for w, _, _ in B]
    sm = SequenceMatcher(None, a, b, autojunk=len(a) * len(b) > 4e8)
    ops = sm.get_opcodes()

    # Merge edits separated by a tiny run of equal words into one readable change.
    groups: list[list[int]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if groups and i1 - groups[-1][1] <= merge_gap and j1 - groups[-1][3] <= merge_gap:
            groups[-1][1], groups[-1][3] = i2, j2
        else:
            groups.append([i1, i2, j1, j2])

    changes: list[TextChange] = []
    cosmetic = 0
    for i1, i2, j1, j2 in groups:
        if "".join(a[i1:i2]) == "".join(b[j1:j2]):
            cosmetic += 1  # e.g. "informa tion" vs "information"
            continue
        off = _span(a_text, A, i1, i2)
        sub = _span(b_text, B, j1, j2)
        kind = "replace" if off and sub else ("delete" if off else "insert")
        severity, reasons = classify(off, sub)
        changes.append(
            TextChange(
                kind=kind,
                official=off,
                submitted=sub,
                context_before=" ".join(w for w, _, _ in B[max(0, j1 - context) : j1]),
                context_after=" ".join(w for w, _, _ in B[j2 : j2 + context]),
                severity=severity,
                reasons=reasons,
            )
        )
    return TextComparison(sm.ratio(), changes, len(a), len(b), cosmetic)


# -- images ------------------------------------------------------------------

DIFF_WIDTH = 1200
CELL = 12  # px at DIFF_WIDTH
# A cell is changed when its (shift-tolerant) mean difference exceeds a threshold
# set from the page's own noise: ~10 for two clean renders, up to 35 for a
# recompressed screenshot. The cap keeps a different page looking different.
MIN_THRESHOLD = 10.0
MAX_THRESHOLD = 35.0
PEAK_FACTOR = 1.8
# Above this share of inked cells differing, it is a different page. Measured on real UGC page 1s:
# genuine screenshots <= 0.008, painted-in edits <= 0.11, other letters on the same template >= 0.37.
SAME_DOCUMENT_MAX_CHANGE = 0.20


@dataclass
class VisualDiff:
    query_page: int
    official_page: int
    aligned: bool
    offset: tuple[int, int]
    coverage: float  # share of the official page height the submission covers
    changed_cells: int
    ink_cells: int
    changed_ratio: float  # share of inked cells inside reported regions
    raw_changed_ratio: float  # share of inked cells above the noise threshold, before any filtering
    boxes: list[tuple[int, int, int, int]]  # (x0, y0, x1, y1) in official-page pixels
    official_size: tuple[int, int]
    # Kept for the HTML report; not serialised.
    official_image: Image.Image | None = field(default=None, repr=False)
    query_image: Image.Image | None = field(default=None, repr=False)

    @property
    def same_document(self) -> bool:
        return self.aligned and self.raw_changed_ratio <= SAME_DOCUMENT_MAX_CHANGE

    def to_dict(self) -> dict:
        return {
            "query_page": self.query_page,
            "official_page": self.official_page,
            "aligned": self.aligned,
            "offset": list(self.offset),
            "coverage": self.coverage,
            "changed_cells": self.changed_cells,
            "ink_cells": self.ink_cells,
            "changed_ratio": self.changed_ratio,
            "raw_changed_ratio": self.raw_changed_ratio,
            "boxes": [list(b) for b in self.boxes],
            "official_size": list(self.official_size),
            "same_document": self.same_document,
        }


def _prep(img: Image.Image, width: int) -> Image.Image:
    g = img.convert("L")
    if g.width != width:
        g = g.resize((width, max(1, round(g.height * width / g.width))), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(g, cutoff=0.5)


def _row_profile(arr: np.ndarray, step: int) -> np.ndarray:
    h = arr.shape[0] // step * step
    dark = 255.0 - arr[:h].astype(np.float32)
    return dark.reshape(h // step, step, -1).mean(axis=(1, 2))


def _best_vertical_offset(tall: np.ndarray, short: np.ndarray, step: int = 4) -> tuple[int, float]:
    """Where ``short`` best fits inside ``tall`` vertically, by normalised cross-correlation of row darkness."""
    pt, ps = _row_profile(tall, step), _row_profile(short, step)
    if len(ps) == 0 or len(pt) < len(ps):
        return 0, 0.0
    ps = ps - ps.mean()
    norm_s = np.linalg.norm(ps) or 1.0
    best, best_score = 0, -1.0
    for off in range(len(pt) - len(ps) + 1):
        win = pt[off : off + len(ps)]
        win = win - win.mean()
        score = float(win @ ps) / ((np.linalg.norm(win) or 1.0) * norm_s)
        if score > best_score:
            best, best_score = off, score
    return best * step, best_score


def _fine_shift(a: np.ndarray, b: np.ndarray, radius: int = 8) -> tuple[int, int]:
    """Small (dx, dy) that best aligns b to a, searched on a half-resolution copy."""
    a2, b2 = a[::2, ::2].astype(np.float32), b[::2, ::2].astype(np.float32)
    r = max(1, radius // 2)
    h, w = a2.shape
    best, best_err = (0, 0), np.inf
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            ya, yb = max(0, dy), max(0, -dy)
            xa, xb = max(0, dx), max(0, -dx)
            hh, ww = h - abs(dy), w - abs(dx)
            if hh <= 10 or ww <= 10:
                continue
            err = np.abs(a2[ya : ya + hh, xa : xa + ww] - b2[yb : yb + hh, xb : xb + ww]).mean()
            if err < best_err:
                best, best_err = (dx * 2, dy * 2), err
    return best


def _shift(arr: np.ndarray, dx: int, dy: int, fill: int = 255) -> np.ndarray:
    out = np.full_like(arr, fill)
    h, w = arr.shape
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    out[yd : yd + h - abs(dy), xd : xd + w - abs(dx)] = arr[ys : ys + h - abs(dy), xs : xs + w - abs(dx)]
    return out


def _components(mask: np.ndarray, reach: int = 2) -> list[list[tuple[int, int]]]:
    """Group changed cells that lie within ``reach`` cells of each other."""
    seen = np.zeros_like(mask, dtype=bool)
    groups = []
    h, w = mask.shape
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        stack, group = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            group.append((cy, cx))
            for ny in range(max(0, cy - reach), min(h, cy + reach + 1)):
                for nx in range(max(0, cx - reach), min(w, cx + reach + 1)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        groups.append(group)
    return groups


def _tolerant_cell_diff(a: np.ndarray, b: np.ndarray, cell: int, reach: int = 2) -> np.ndarray:
    """Per-cell mean absolute difference, minimised over small shifts of ``b``.

    Rescaling a screenshot drifts glyph edges by a pixel or two across the
    page; letting each cell find its best local shift removes that noise
    while a changed digit or word still differs at every shift.
    """
    h, w = a.shape
    hc, wc = h // cell, w // cell
    padded = np.pad(b, reach, mode="edge")
    best = None
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            shifted = padded[reach + dy : reach + dy + h, reach + dx : reach + dx + w]
            d = np.abs(a - shifted)[: hc * cell, : wc * cell].reshape(hc, cell, wc, cell).mean(axis=(1, 3))
            best = d if best is None else np.minimum(best, d)
    return best


def compare_images(official: Image.Image, query: Image.Image, *, query_page: int = 1,
                   official_page: int = 1, keep_images: bool = True) -> VisualDiff:
    # Compare at the submission's own resolution (capped), so a low-resolution
    # screenshot is not penalised for being blurrier than a fresh render.
    work = int(max(400, min(DIFF_WIDTH, query.width)))
    scale_out = DIFF_WIDTH / work
    cell = max(6, round(CELL * work / DIFF_WIDTH))
    o = np.asarray(_prep(official, work))
    q = np.asarray(_prep(query, work))

    # Vertical placement: the submission may be a crop (screenshot of half a
    # page) or have extra margins. Fit the shorter image inside the taller one.
    corr = 1.0
    if abs(o.shape[0] - q.shape[0]) > 0.02 * o.shape[0]:
        if q.shape[0] < o.shape[0]:
            off, corr = _best_vertical_offset(o, q)
            o_win, q_win, y0 = o[off : off + q.shape[0]], q, off
        else:
            off, corr = _best_vertical_offset(q, o)
            o_win, q_win, y0 = o, q[off : off + o.shape[0]], 0
    else:
        o_win, q_win, y0 = o, q, 0
    # Row profiles are computed in steps of a few pixels, so the windows can
    # disagree by a row or two at the bottom; trim both to the shared height.
    h = min(o_win.shape[0], q_win.shape[0])
    o_win, q_win = o_win[:h], q_win[:h]

    dx, dy = _fine_shift(o_win, q_win)
    q_al = _shift(q_win, dx, dy)
    coverage = o_win.shape[0] / o.shape[0]

    a = np.asarray(Image.fromarray(o_win).filter(ImageFilter.GaussianBlur(1.0)), dtype=np.float32)
    b = np.asarray(Image.fromarray(q_al).filter(ImageFilter.GaussianBlur(1.0)), dtype=np.float32)
    diff = _tolerant_cell_diff(a, b, cell)
    hc, wc = diff.shape
    ink = np.minimum(a, b)[: hc * cell, : wc * cell].reshape(hc, cell, wc, cell).min(axis=(1, 3)) < 170
    # Ignore a border strip where the shift leaves filler pixels.
    border = max(1, (max(abs(dx), abs(dy)) + cell - 1) // cell)
    valid = np.zeros_like(ink)
    valid[border:-border or None, border:-border or None] = True
    considered = ink & valid
    noise = float(np.percentile(diff[considered], 90)) if considered.any() else 0.0
    threshold = float(np.clip(2.0 * noise + 4.0, MIN_THRESHOLD, MAX_THRESHOLD))
    changed = (diff > threshold) & considered
    raw_changed = int(changed.sum())
    strong = diff > 2.5 * threshold

    # Drop isolated single cells (JPEG speckle) unless the change is strong.
    if changed.any():
        padded = np.pad(changed, 1)
        neighbours = sum(np.roll(np.roll(padded, sy, 0), sx, 1)
                         for sy in (-1, 0, 1) for sx in (-1, 0, 1) if sy or sx)[1:-1, 1:-1]
        changed &= (neighbours > 0) | strong

    boxes = []
    n_changed = 0
    for group in _components(changed):
        if len(group) < 2 and not strong[group[0]]:
            continue
        # A real edit has at least one cell well above the noise threshold;
        # recompression noise on fine print peaks at about 1.6x on real pages.
        if max(diff[g] for g in group) < PEAK_FACTOR * threshold:
            continue
        n_changed += len(group)
        ys = [g[0] for g in group]
        xs = [g[1] for g in group]
        boxes.append(tuple(int(round(v * scale_out)) for v in (
            min(xs) * cell, y0 + min(ys) * cell, (max(xs) + 1) * cell, y0 + (max(ys) + 1) * cell)))
    n_ink = int((ink & valid).sum())

    official_img = query_img = None
    if keep_images:
        official_img = _prep(official, DIFF_WIDTH)
        placed = Image.fromarray(_place(q_al, o.shape, y0))
        query_img = placed.resize(official_img.size, Image.Resampling.LANCZOS)
    return VisualDiff(
        query_page=query_page,
        official_page=official_page,
        aligned=bool(corr >= 0.5),
        offset=(int(round(dx * scale_out)), int(round((dy + y0) * scale_out))),
        coverage=round(float(coverage), 3),
        changed_cells=int(n_changed),
        ink_cells=n_ink,
        changed_ratio=round(n_changed / max(n_ink, 1), 4),
        raw_changed_ratio=round(raw_changed / max(n_ink, 1), 4),
        boxes=sorted(boxes, key=lambda bx: (bx[1], bx[0])),
        official_size=(DIFF_WIDTH, int(round(o.shape[0] * scale_out))),
        official_image=official_img,
        query_image=query_img,
    )


def _place(win: np.ndarray, shape: tuple[int, int], y0: int) -> np.ndarray:
    """Put the aligned submission window back on a full-page canvas for side-by-side display."""
    canvas = np.full(shape, 255, dtype=np.uint8)
    h = min(win.shape[0], shape[0] - y0)
    canvas[y0 : y0 + h, : win.shape[1]] = win[:h, : shape[1]]
    return canvas
