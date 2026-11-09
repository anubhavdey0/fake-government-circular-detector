"""Measure the verifier on real documents from the index.

There are no real forged circulars to train or test on, and this project does
not need any: it never learns what a forgery looks like. It compares against
what the issuer published. So the questions worth measuring are:

* Does a genuine copy, however it was re-saved or screenshotted, still match?
* Does an edit to a real document get flagged, and is the right spot marked?
* Does a fabricated notice on a real letterhead come back as "not found"?
* What happens to a genuine document the index has not seen (stale index)?

Each sampled official file is turned into the variants below and verified.

Usage:  python scripts/evaluate.py --n 100 [--data-dir DIR] [--out docs/evaluation.md]
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageDraw

from circverify.config import default_data_dir
from circverify.db import Database
from circverify.extract import render_pdf_page
from circverify.matcher import ALTERED, NO_MATCH, VERIFIED_CONTENT, VERIFIED_EXACT, Verifier
from circverify.store import BlobStore

EXPECT = {
    "exact copy": {VERIFIED_EXACT},
    "page 1 saved as new PDF": {VERIFIED_CONTENT, VERIFIED_EXACT},
    "WhatsApp-style JPEG screenshot": {VERIFIED_CONTENT},
    "screenshot with a date painted in": {ALTERED},
    "PDF edited: white-out + typed text": {ALTERED},
    "letterhead kept, body fabricated": {NO_MATCH},
}


# -- variants ---------------------------------------------------------------

def jpeg(img: Image.Image, width: int, quality: int) -> bytes:
    img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def text_line_band(img: Image.Image, rng: random.Random) -> tuple[int, int, int, int] | None:
    """A text line in the middle of the page: (x0, y0, x1, y1) in pixels."""
    a = np.asarray(img.convert("L"))
    h, w = a.shape
    dark_rows = (a < 128).sum(axis=1) > w * 0.02
    runs, start = [], None
    for y in range(int(h * 0.25), int(h * 0.80)):
        if dark_rows[y] and start is None:
            start = y
        elif not dark_rows[y] and start is not None:
            if 8 <= y - start <= 45:
                runs.append((start, y))
            start = None
    if not runs:
        return None
    y0, y1 = rng.choice(runs)
    cols = np.nonzero((a[y0:y1] < 128).any(axis=0))[0]
    if cols.size < 20:
        return None
    x0, x1 = int(cols.min()), int(cols.max())
    seg = max(60, int((x1 - x0) * 0.3))
    sx = rng.randint(x0, max(x0, x1 - seg))
    return sx, y0 - 2, min(w - 1, sx + seg), y1 + 2


def painted_date(page: Image.Image, rng: random.Random):
    band = text_line_band(page, rng)
    if band is None:
        return None
    img = page.convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle(band, fill="white")
    d.text((band[0] + 2, band[1] + 1), "31.12.2026", fill="black", font_size=max(10, band[3] - band[1] - 4))
    return jpeg(img, 1000, 80), band


def letterhead_fabrication(page: Image.Image) -> bytes:
    img = page.convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    d.rectangle((0, int(h * 0.22), w, h), fill="white")
    body = [
        "Subject: One-time scholarship for all registered students",
        "All students who register on the portal below before 30 November",
        "will receive a scholarship of Rs. 25,000 directly in their bank account.",
        "A registration fee of Rs. 199 is payable online at www.ugc-scholarship.org.in",
        "This notice is issued with the approval of the competent authority.",
    ]
    size = max(12, w // 55)
    for i, line in enumerate(body):
        d.text((int(w * 0.1), int(h * 0.28) + i * int(size * 1.8)), line, fill="black", font_size=size)
    return jpeg(img, 1000, 80)


def pdf_whiteout_edit(data: bytes, band_px, render_width: int) -> bytes | None:
    """Page 1 as a new PDF with a white box and new text drawn over it, as online PDF editors do."""
    src = pdfium.PdfDocument(data)
    out = pdfium.PdfDocument.new()
    out.import_pages(src, [0])
    page = out[0]
    w_pt, h_pt = page.get_width(), page.get_height()
    s = w_pt / render_width
    x0, y0, x1, y1 = (v * s for v in band_px)
    rect = pdfium_c.FPDFPageObj_CreateNewRect(x0, h_pt - y1, x1 - x0, y1 - y0)
    pdfium_c.FPDFPageObj_SetFillColor(rect, 255, 255, 255, 255)
    pdfium_c.FPDFPath_SetDrawMode(rect, pdfium_c.FPDF_FILLMODE_ALTERNATE, False)
    pdfium_c.FPDFPage_InsertObject(page.raw, rect)
    size = max(6.0, (y1 - y0) * 0.75)
    text = pdfium_c.FPDFPageObj_NewTextObj(out.raw, b"Helvetica", size)
    buf = ctypes.create_string_buffer("31 December 2026".encode("utf-16-le") + b"\x00\x00")
    pdfium_c.FPDFText_SetText(text, ctypes.cast(buf, ctypes.POINTER(pdfium_c.FPDF_WCHAR)))
    pdfium_c.FPDFPageObj_Transform(text, 1, 0, 0, 1, x0 + 1, h_pt - y1 + (y1 - y0) * 0.2)
    pdfium_c.FPDFPage_InsertObject(page.raw, text)
    if not pdfium_c.FPDFPage_GenerateContent(page.raw):
        return None
    dest = io.BytesIO()
    out.save(dest)
    page.close()
    out.close()
    src.close()
    return dest.getvalue()


def first_page_pdf(data: bytes) -> bytes:
    src = pdfium.PdfDocument(data)
    out = pdfium.PdfDocument.new()
    out.import_pages(src, [0])
    dest = io.BytesIO()
    out.save(dest)
    out.close()
    src.close()
    return dest.getvalue()


def overlaps(boxes, band, scale) -> bool:
    bx0, by0, bx1, by1 = (v * scale for v in band)
    return any(x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1 for x0, y0, x1, y1 in boxes)


# -- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100, help="official files to sample")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="write a Markdown summary here")
    ap.add_argument("--json", type=Path, default=None, help="write per-case results here")
    args = ap.parse_args()

    root = args.data_dir or default_data_dir()
    db = Database(root / "index.sqlite3")
    store = BlobStore(root)
    rows = db.conn.execute(
        "SELECT sha256, n_pages, text_pages FROM files WHERE kind='pdf' AND index_version > 0 "
        "AND index_error IS NULL AND n_pages > 0 ORDER BY sha256").fetchall()
    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))
    print(f"Sampling {len(sample)} of {len(rows)} indexed PDFs", file=sys.stderr)

    verifier = Verifier(db, store, use_ocr=False)
    cases = []
    timings = []

    def run(name, sha, data, *, scanned, band=None, scale=None, filename=None, hide=None):
        t = time.perf_counter()
        res = verifier.verify(data, filename, exclude=hide)
        timings.append(time.perf_counter() - t)
        localised = None
        if band is not None and res.match is not None:
            boxes = [b for vd in res.match.visual for b in vd.boxes]
            localised = overlaps(boxes, band, scale) if boxes else False
        matched_self = bool(res.match and res.match.sha256 == sha)
        cases.append({"variant": name, "sha256": sha, "scanned": scanned, "verdict": res.verdict,
                      "matched_source": matched_self, "localised": localised,
                      "summary": res.summary})

    for i, row in enumerate(sample, 1):
        sha = row["sha256"]
        data = store.get(sha)
        scanned = (row["text_pages"] or 0) == 0
        try:
            page = render_pdf_page(data, 1, 1000)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {sha[:12]}: {exc}", file=sys.stderr)
            continue
        inked = (np.asarray(page) < 128).mean() > 0.003
        run("exact copy", sha, data, scanned=scanned)
        run("page 1 saved as new PDF", sha, first_page_pdf(data), scanned=scanned)
        if inked:
            run("WhatsApp-style JPEG screenshot", sha, jpeg(page, 720, 60), scanned=scanned, filename="fwd.jpg")
            painted = painted_date(page, rng)
            if painted:
                blob, band = painted
                run("screenshot with a date painted in", sha, blob, scanned=scanned, band=band, scale=1.2)
                edited = pdf_whiteout_edit(data, band, 1000)
                if edited:
                    run("PDF edited: white-out + typed text", sha, edited, scanned=scanned, band=band, scale=1.2)
            run("letterhead kept, body fabricated", sha, letterhead_fabrication(page), scanned=scanned)
        run("held out of the index (genuine, unseen)", sha, data, scanned=scanned, hide={sha})
        if i % 10 == 0:
            print(f"  {i}/{len(sample)}", file=sys.stderr)

    # -- summary --
    by = defaultdict(list)
    for c in cases:
        by[c["variant"]].append(c)
    lines = ["| Variant | Cases | Expected verdict | Correct | Text-layer PDFs | Scanned PDFs |",
             "|---|---:|---|---:|---:|---:|"]
    for name, expect in EXPECT.items():
        cs = by.get(name, [])
        if not cs:
            continue

        def rate(subset):
            if not subset:
                return "n/a"
            ok = sum(c["verdict"] in expect for c in subset)
            return f"{ok}/{len(subset)} ({ok / len(subset):.0%})"

        text = [c for c in cs if not c["scanned"]]
        scan = [c for c in cs if c["scanned"]]
        lines.append(f"| {name} | {len(cs)} | {' or '.join(sorted(expect))} | {rate(cs)} | {rate(text)} | "
                     f"{rate(scan)} |")
    edits = [c for c in cases if c["localised"] is not None and c["verdict"] == ALTERED]
    loc = sum(c["localised"] for c in edits)
    held = by.get("held out of the index (genuine, unseen)", [])
    held_counts = Counter(c["verdict"] for c in held)

    out = ["## Evaluation on real UGC documents", "",
           f"{len(sample)} official PDFs sampled at random (seed {args.seed}) from {len(rows):,} indexed. "
           "No OCR. Each file was turned into the variants below and checked against the full index.", "",
           *lines, "",
           f"Of the edited copies flagged as altered, **{loc}/{len(edits)}** had a marked region overlapping the "
           "edit.", "",
           "**Genuine documents missing from the index.** Each sampled file was also checked with itself "
           "removed from the index, which is what happens when a real notice is newer than the last crawl:", ""]
    for verdict, n in held_counts.most_common():
        out.append(f"- {verdict}: {n} ({n / len(held):.0%})")
    out += ["", "`VERIFIED_CONTENT` here means the issuer published the same document again under another "
            "entry (common for re-posted notices). `ALTERED` means a close relative exists, usually the "
            "same letter issued for another year, which is why the report tells you to refresh the index "
            "before concluding a recent notice was edited.", "",
            f"Median time per check: {statistics.median(timings):.2f} s "
            f"(90th percentile {sorted(timings)[int(len(timings) * 0.9)]:.2f} s)."]
    report = "\n".join(out)
    print(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(cases, indent=1), encoding="utf-8")
    misses = [c for c in cases if c["variant"] in EXPECT and c["verdict"] not in EXPECT[c["variant"]]]
    for c in misses[:25]:
        print(f"MISS {c['variant']}: {c['sha256'][:12]} -> {c['verdict']}: {c['summary'][:140]}", file=sys.stderr)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
