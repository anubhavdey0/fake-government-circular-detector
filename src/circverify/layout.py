"""How unusual a first page's layout is, compared with the issuer's own documents.

No forged documents are needed: the model is the issuer's real first pages. Each page is reduced to
a dozen layout measurements (margins, ink in the letterhead band and the footer, number and spacing
of text lines, horizontal rules). A submission is scored by its distance to the nearest real pages,
and that distance is turned into a percentile using the real pages themselves: "stranger than 99.7%
of the issuer's own documents". The features that differ most are named, so the hint is readable.

This is a nearest-neighbour anomaly score rather than an isolation forest: the issuer's documents
come in several families (letters, tables, scanned notices, reports), which nearest neighbours handle
without tuning, and it needs nothing beyond numpy. It is a hint for an unverified document only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
from PIL import Image

FEATURES = ("aspect", "margin_left", "margin_right", "margin_top", "margin_bottom", "ink", "ink_header",
            "ink_footer", "lines", "line_gap", "text_width", "rules")
LABELS = {
    "aspect": "page shape", "margin_left": "left margin", "margin_right": "right margin",
    "margin_top": "top margin", "margin_bottom": "bottom margin", "ink": "amount of ink",
    "ink_header": "ink in the letterhead band", "ink_footer": "ink in the footer band",
    "lines": "number of text lines", "line_gap": "line spacing", "text_width": "width of the text block",
    "rules": "horizontal rules",
}
WIDTH = 256
K = 5
UNUSUAL = 0.995  # percentile of the issuer's own pages above which a layout is called unusual


def layout_features(image: Image.Image) -> list[float] | None:
    """Layout measurements of a page image, or None for a blank page."""
    g = image.convert("L")
    h = max(1, round(g.height * WIDTH / g.width))
    a = np.asarray(g.resize((WIDTH, h), Image.Resampling.BILINEAR)) < 160
    if a.mean() < 0.002 or h < 32:
        return None
    cols, rows = a.mean(axis=0), a.mean(axis=1)
    ink_cols, ink_rows = np.flatnonzero(cols > 0.002), np.flatnonzero(rows > 0.002)
    if not ink_cols.size or not ink_rows.size:
        return None
    on = rows > 0.01
    starts = np.flatnonzero(on & ~np.concatenate(([False], on[:-1])))
    ends = np.flatnonzero(on & ~np.concatenate((on[1:], [False])))
    runs = [(s, e) for s, e in zip(starts, ends) if e - s >= 1]
    gaps = np.diff([s for s, _ in runs]) if len(runs) > 1 else np.array([0])
    return [
        round(h / WIDTH, 4),
        round(ink_cols[0] / WIDTH, 4),
        round(1 - (ink_cols[-1] + 1) / WIDTH, 4),
        round(ink_rows[0] / h, 4),
        round(1 - (ink_rows[-1] + 1) / h, 4),
        round(float(a.mean()), 4),
        round(float(a[: max(1, int(0.22 * h))].mean()), 4),
        round(float(a[int(0.88 * h):].mean()), 4),
        round(len(runs) * 100 / h, 4),
        round(float(np.median(gaps)) / h, 4),
        round((ink_cols[-1] - ink_cols[0] + 1) / WIDTH, 4),
        round(float((rows > 0.5).sum()) * 100 / h, 4),
    ]


def dumps(features: list[float] | None) -> str:
    return json.dumps(features) if features else ""


@dataclass
class LayoutScore:
    percentile: float  # share of the issuer's own pages that are less unusual
    unusual: bool
    differences: list[tuple[str, float, float]]  # (feature, submitted value, issuer median)


class LayoutModel:
    """Nearest-neighbour distances to the issuer's first pages, calibrated on those pages."""

    def __init__(self, vectors: np.ndarray, *, sample: int = 1500, seed: int = 7):
        self.n = len(vectors)
        self.median = np.median(vectors, axis=0)
        mad = np.median(np.abs(vectors - self.median), axis=0) * 1.4826
        spread = np.percentile(vectors, 90, axis=0) - np.percentile(vectors, 10, axis=0)
        self.scale = np.maximum(mad, np.maximum(spread / 6, 1e-3))
        self.z = self._z(vectors)
        rng = np.random.default_rng(seed)
        idx = rng.choice(self.n, size=min(sample, self.n), replace=False)
        self.reference = np.sort([self._knn(self.z[i], exclude=i) for i in idx])

    @classmethod
    def from_rows(cls, rows) -> LayoutModel | None:
        vecs = []
        for (raw,) in rows:
            try:
                v = json.loads(raw) if raw else None
            except ValueError:
                v = None
            if v and len(v) == len(FEATURES):
                vecs.append(v)
        return cls(np.array(vecs, dtype=np.float64)) if len(vecs) >= 50 else None

    def _z(self, x: np.ndarray) -> np.ndarray:
        return np.clip((x - self.median) / self.scale, -8, 8)

    def _knn(self, z: np.ndarray, exclude: int | None = None) -> float:
        d = np.sqrt(((self.z - z) ** 2).sum(axis=1))
        if exclude is not None:
            d[exclude] = np.inf
        return float(np.partition(d, K)[K - 1]) if len(d) > K else float(d.min())

    def score(self, features: list[float]) -> LayoutScore:
        z = self._z(np.asarray(features, dtype=np.float64))
        dist = self._knn(z)
        pct = float(np.searchsorted(self.reference, dist) / len(self.reference))
        order = np.argsort(-np.abs(z))
        diffs = [(FEATURES[i], float(features[i]), float(self.median[i])) for i in order[:3] if abs(z[i]) >= 3]
        return LayoutScore(round(pct, 4), pct >= UNUSUAL, diffs)


def describe(score: LayoutScore, issuer: str = "UGC") -> str | None:
    if not score.unusual:
        return None
    named = ", ".join(LABELS[f] for f, _, _ in score.differences[:2])
    detail = f" The biggest differences: {named}." if named else ""
    return (f"Its page layout is unlike {issuer}'s documents: it is further from its closest {issuer} pages than "
            f"{score.percentile:.1%} of {issuer}'s own documents are from theirs.{detail} Layouts vary, so this is "
            "only a hint.")
