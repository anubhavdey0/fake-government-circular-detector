"""Five targeted edits on real UGC circulars, and whether each is caught and located.

For each sampled circular (text-layer PDFs, so the position of every character is known), page 1
is rendered and five edited copies are made, each touching one element a forger would change:

1. the issue date,
2. a deadline or other date in the body,
3. the reference number,
4. one line of the body, rewritten,
5. the signature, replaced by another scribble.

Each copy is saved as a WhatsApp-quality JPEG and checked against the full index (no OCR). Reported
per edit: how often the verdict is Altered (a Verified here would be a false verification), how
often a marked region overlaps the edit, and the mean intersection-over-union of the best marked
region with the edited area. These are synthetic, controlled edits: a research dataset, not
real-world forgeries.

Usage:  python scripts/tamper_benchmark.py --n 40 --out docs/tampering.md --csv docs/tampering-cases.csv
        [--demo docs/tampering-demo.jpg]
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from evaluate import jpeg, text_line_band  # noqa: E402

from circverify.config import default_data_dir  # noqa: E402
from circverify.db import Database  # noqa: E402
from circverify.fields import _REF, find_dates  # noqa: E402
from circverify.matcher import ALTERED, UNCONFIRMED_MATCH, VERIFIED_CONTENT, VERIFIED_EXACT, Verifier  # noqa: E402
from circverify.store import BlobStore  # noqa: E402

WIDTH = 1000
DIFF_SCALE = 1.2  # marked regions are reported at 1200 px wide
EDITS = ("issue date", "date in the body", "reference number", "rewritten line", "signature")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


class Page:
    """Page 1 rendered at WIDTH px with a map from text positions to pixel boxes."""

    def __init__(self, data: bytes):
        pdf = pdfium.PdfDocument(data)
        page = pdf[0]
        if page.get_rotation():
            raise ValueError("rotated page")
        w_pt, h_pt = page.get_size()
        self.scale = WIDTH / w_pt
        self.h_pt = h_pt
        self.image = page.render(scale=self.scale).to_pil().convert("RGB")
        tp = page.get_textpage()
        self.text = tp.get_text_range()
        n = tp.count_chars()
        self.boxes = [tp.get_charbox(i, loose=True) for i in range(n)]
        if abs(len(self.text) - n) > 2:
            raise ValueError("text and characters do not line up")

    def box(self, start: int, end: int) -> tuple[int, int, int, int] | None:
        rects = [self.boxes[i] for i in range(start, min(end, len(self.boxes)))
                 if self.boxes[i][2] > self.boxes[i][0] and not self.text[i].isspace()]
        if not rects:
            return None
        x0, x1 = min(r[0] for r in rects), max(r[2] for r in rects)
        top, bottom = max(r[3] for r in rects), min(r[1] for r in rects)
        b = (int(x0 * self.scale) - 2, int((self.h_pt - top) * self.scale) - 2,
             int(x1 * self.scale) + 2, int((self.h_pt - bottom) * self.scale) + 2)
        if b[2] - b[0] < 8 or b[3] - b[1] < 6 or b[3] - b[1] > 60:
            return None
        return b


def _paint(img: Image.Image, box, text: str) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    d.rectangle(box, fill="white")
    d.text((box[0] + 1, box[1]), text, fill="black", font_size=max(9, int((box[3] - box[1]) * 0.85)))
    return out


def _shift_date(iso: str, days: int) -> str:
    d = date.fromisoformat(iso) + timedelta(days=days)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _scribble(img: Image.Image, box, rng: random.Random) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    d.rectangle(box, fill="white")
    x0, y0, x1, y1 = box
    pts = []
    for i in range(14):
        t = i / 13
        pts.append((x0 + 6 + t * (x1 - x0 - 12), y0 + 6 + rng.random() * (y1 - y0 - 12)))
    d.line(pts, fill=(20, 30, 90), width=3, joint="curve")
    d.line([(x0 + 10, y1 - 8), (x1 - 10, y1 - 12)], fill=(20, 30, 90), width=2)
    return out


def make_edits(data: bytes, fields, rng: random.Random) -> dict[str, tuple[Image.Image, tuple]]:
    page = Page(data)
    text, img = page.text, page.image
    out: dict[str, tuple[Image.Image, tuple]] = {}
    dates = find_dates(text)
    if dates:
        pos, iso = dates[0]
        m = re.match(r"[^\n]{0,30}?\d{4}", text[pos:pos + 40])
        b = page.box(pos, pos + (m.end() if m else 10))
        if b:
            out["issue date"] = (_paint(img, b, _shift_date(iso, -20)), b)
    body = [(p, i) for p, i in dates[1:] if i != (dates[0][1] if dates else None)]
    if body:
        pos, iso = body[0]
        m = re.match(r"[^\n]{0,30}?\d{4}", text[pos:pos + 40])
        b = page.box(pos, pos + (m.end() if m else 10))
        if b:
            out["date in the body"] = (_paint(img, b, _shift_date(iso, 15)), b)
    m = _REF.search(text)
    if m:
        b = page.box(m.start(1), m.end(1))
        if b:
            num = re.sub(r"\d+", lambda x: str(int(x.group()) + 7), m.group(1).split("/")[0], count=1)
            out["reference number"] = (_paint(img, b, num + "/" + m.group(1).split("/", 1)[1]), b)
    band = text_line_band(img.convert("L"), rng)
    if band:
        out["rewritten line"] = (_paint(img, band, "All students must pay a fee of Rs. 2,500 by 31 December 2026."), band)
    sig = (fields["signatory"] if fields else None) or ""
    at = text.find(sig) if len(sig) >= 4 else -1
    if at >= 0:
        nb = page.box(at, at + len(sig))
        if nb:
            h = nb[3] - nb[1]
            sb = (max(0, nb[0] - 20), max(0, nb[1] - 4 * h), min(WIDTH, nb[2] + 20), nb[1] - 2)
            if sb[3] - sb[1] > 20:
                out["signature"] = (_scribble(img, sb, rng), sb)
    return out


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--demo", type=Path, default=None, help="save the five edits of one circular, marked, as a JPEG")
    args = ap.parse_args()

    root = default_data_dir()
    db, store = Database(root / "index.sqlite3"), BlobStore(root)
    verifier = Verifier(db, store, use_ocr=False)
    rows = db.conn.execute(
        "SELECT f.sha256 FROM files f JOIN fields x ON x.sha256 = f.sha256 WHERE f.kind = 'pdf' AND f.text_pages >= 1"
        " AND f.index_error IS NULL AND x.ref_no IS NOT NULL AND x.doc_date IS NOT NULL AND x.signatory IS NOT NULL"
        " AND f.sha256 IN ("
        " SELECT fe.sha256 FROM fetches fe JOIN links l ON l.url = fe.url JOIN notices n ON n.id = l.notice_id"
        " WHERE fe.status = 'ok' AND n.source != 'archive') ORDER BY f.sha256").fetchall()
    rng = random.Random(args.seed)
    order = [r[0] for r in rows]
    rng.shuffle(order)
    cases, docs, demo = [], 0, None
    for sha in order:
        if docs >= args.n:
            break
        try:
            edits = make_edits(store.get(sha), db.fields(sha), rng)
        except Exception:  # noqa: BLE001 - unusual PDFs (rotated, odd text layers) are skipped
            continue
        if len(edits) < 3:
            continue
        docs += 1
        marked = {}
        for name, (img, box) in edits.items():
            res = verifier.verify(jpeg(img, None, 80), f"{name}.jpg")
            truth = tuple(v * DIFF_SCALE for v in box)
            same = [v for v in (res.match.visual if res.match else []) if v.same_document]
            boxes = [b for v in same for b in v.boxes]
            to_check = [b for v in same for b in v.lines_to_check]
            best = max((iou(truth, b) for b in boxes + to_check), default=0.0)
            hit = any(iou(truth, b) > 0 or (b[0] < truth[2] and b[2] > truth[0] and b[1] < truth[3] and b[3] > truth[1])
                      for b in boxes + to_check)
            cases.append({"source_sha256": sha, "edit": name, "verdict": res.verdict, "caught": res.verdict == ALTERED,
                          "unconfirmed": res.verdict == UNCONFIRMED_MATCH,
                          "false_verified": res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT), "marked": hit,
                          "iou": round(best, 3)})
            marked[name] = (img, box, boxes, to_check)
        if demo is None and len(marked) == len(EDITS):
            demo = marked
        if docs % 10 == 0:
            print(f"  {docs}/{args.n}", file=sys.stderr)

    by = defaultdict(list)
    for c in cases:
        by[c["edit"]].append(c)
    lines = ["| Edit | Cases | Altered | Matches, details unconfirmed | Still called Verified "
             "| Region marked on the edit | Mean IoU (when marked) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name in EDITS:
        cs = by.get(name, [])
        if not cs:
            continue
        n = len(cs)
        caught = sum(c["caught"] for c in cs)
        fv = sum(c["false_verified"] for c in cs)
        unc = sum(c["unconfirmed"] for c in cs)
        hit = [c for c in cs if c["marked"]]
        miou = statistics.mean(c["iou"] for c in hit) if hit else 0.0
        lines.append(f"| {name} | {n} | {caught}/{n} ({caught / n:.0%}) | {unc} | {fv} | {len(hit)}/{n} ({len(hit) / n:.0%}) "
                     f"| {miou:.2f} |")
    total = len(cases)
    fv_all = sum(c["false_verified"] for c in cases)
    report = "\n".join([
        "## Targeted tampering", "",
        f"{docs} text-layer UGC circulars with a reference number, date and signatory line, sampled at random "
        f"(seed {args.seed}); five edits each where the element "
        "could be located, saved as WhatsApp-quality JPEGs and checked against the full index without OCR. "
        "Synthetic, controlled edits.", "",
        *lines, "",
        f"Overall: {sum(c['caught'] for c in cases)}/{total} edited copies called Altered; "
        f"{sum(c['unconfirmed'] for c in cases)} called \"Matches, details unconfirmed\"; {fv_all} still called "
        "Verified. A marked region counts the red changed regions and the amber lines to check.", ""])
    print(report)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["seed", *cases[0].keys()])
            w.writeheader()
            for c in cases:
                w.writerow({"seed": args.seed, **c})
    if args.demo and demo:
        tiles = []
        for name in EDITS:
            img, box, boxes, to_check = demo[name]
            t = img.copy()
            d = ImageDraw.Draw(t)
            for b in boxes:
                d.rectangle([v / DIFF_SCALE for v in b], outline=(220, 38, 38), width=4)
            for b in to_check:
                d.rectangle([v / DIFF_SCALE for v in b], outline=(217, 119, 6), width=4)
            d.rectangle(box, outline=(22, 163, 74), width=2)
            t = t.resize((400, round(t.height * 400 / t.width)))
            ImageDraw.Draw(t).text((10, 8), name, fill=(17, 24, 39), font_size=20)
            tiles.append(t)
        h = max(t.height for t in tiles)
        sheet = Image.new("RGB", (sum(t.width for t in tiles) + 12 * (len(tiles) - 1), h), "white")
        x = 0
        for t in tiles:
            sheet.paste(t, (x, 0))
            x += t.width + 12
        buf = io.BytesIO()
        sheet.save(buf, "JPEG", quality=86)
        args.demo.write_bytes(buf.getvalue())
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
