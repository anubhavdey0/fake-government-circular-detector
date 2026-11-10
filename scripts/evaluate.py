"""Measure the verifier on real documents.

Five groups, as a credible evaluation needs:

1. Real notices from the index, as published            -> should be Verified
2. Real notices forwarded: WhatsApp JPEG, phone
   screenshot with black bars, angled phone photo       -> should be Verified
3. Real notices altered: a date painted into a
   screenshot, a PDF-editor white-out with typed text,
   and a phone photo of an edited printout              -> should be Altered
4. Real-world fakes publicly debunked by PIB Fact Check
   (forwarded variants)                                 -> should be Known fake,
   and with the known-fakes store switched off          -> must never be Verified
5. Genuine circulars from another issuer (NTA)          -> should be Not found, never Verified

Plus two stress groups: fabricated notices on a real UGC letterhead, and
genuine UGC notices removed from the index (what a stale index sees).

The key number is the false-verified rate: how often something that is not
an official UGC document is called Verified. It should be zero.

Altered notices made by people other than the author are the strongest test;
put them in a folder with a ``manifest.csv`` (``file,expected``) and pass
``--external DIR``.

Usage:  python scripts/evaluate.py --n 100 [--others 40] [--external DIR] [--out docs/evaluation.md]
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageDraw, ImageFilter

from circverify.config import default_data_dir
from circverify.crawler import Fetcher
from circverify.db import Database
from circverify.extract import render_pdf_page
from circverify.matcher import (
    ALTERED,
    KNOWN_FAKE,
    NO_MATCH,
    UNCONFIRMED_MATCH,
    VERIFIED_CONTENT,
    VERIFIED_EXACT,
    Verifier,
)
from circverify.store import BlobStore

VERIFIED = {VERIFIED_EXACT, VERIFIED_CONTENT}
GROUPS = {
    "1. Published copy": ("exact copy", "page 1 saved as a new PDF"),
    "2. Forwarded": ("WhatsApp-quality JPEG", "phone screenshot with black bars", "angled phone photo"),
    "3. Altered": ("screenshot with a date painted in", "PDF edited: white-out + typed text",
                   "phone photo of an edited printout"),
    "4. Debunked fakes": ("fake, forwarded copy", "fake, store switched off"),
    "5. Other issuer": ("NTA circular",),
    "Stress": ("UGC letterhead, fabricated body", "genuine UGC notice missing from the index"),
}
EXPECT = {
    "exact copy": {VERIFIED_EXACT},
    "page 1 saved as a new PDF": VERIFIED,
    # A photo matched by its image alone is reported as "matches, details unconfirmed": correct for a genuine
    # copy (the official file is found), and not a verification.
    "WhatsApp-quality JPEG": VERIFIED | {UNCONFIRMED_MATCH},
    "phone screenshot with black bars": VERIFIED | {UNCONFIRMED_MATCH},
    "angled phone photo": VERIFIED | {UNCONFIRMED_MATCH},
    "screenshot with a date painted in": {ALTERED},
    "PDF edited: white-out + typed text": {ALTERED},
    "phone photo of an edited printout": {ALTERED},
    "fake, forwarded copy": {KNOWN_FAKE},
    "fake, store switched off": {NO_MATCH, ALTERED},
    "NTA circular": {NO_MATCH},
    "UGC letterhead, fabricated body": {NO_MATCH},
    "genuine UGC notice missing from the index": {NO_MATCH, ALTERED, VERIFIED_CONTENT, UNCONFIRMED_MATCH},
}
# Variants that are not an official UGC document as submitted: a Verified here is a false verification.
NOT_OFFICIAL = {"screenshot with a date painted in", "PDF edited: white-out + typed text",
                "phone photo of an edited printout", "fake, forwarded copy", "fake, store switched off",
                "NTA circular", "UGC letterhead, fabricated body"}


def is_correct(c: dict) -> bool:
    if c["variant"] == "NTA circular" and c["note"] == "also published by UGC":
        return c["verdict"] in VERIFIED
    return c["verdict"] in EXPECT.get(c["variant"], set())


def write_cases_csv(cases: list[dict], path: Path, seed: int) -> None:
    """One row per case, so the test set can be published and re-run without sharing any document.

    Official files are identified by SHA-256 (all listed in manifest/ugc.csv); the variants are
    regenerated deterministically from the seed by this script.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["seed", "variant", "source_sha256", "scanned", "note", "expected", "verdict", "correct",
                    "edit_marked", "edit_iou", "original_rank"])
        for c in cases:
            found = c.get("found") or []
            rank = found.index(c["sha256"]) + 1 if c["sha256"] and c["sha256"] in found else ""
            w.writerow([seed, c["variant"], c["sha256"] or "", "" if c["scanned"] is None else int(c["scanned"]),
                        c["note"] or "", " or ".join(sorted(EXPECT.get(c["variant"], ()))), c["verdict"],
                        int(is_correct(c)), "" if c["localised"] is None else int(c["localised"]),
                        "" if c.get("iou") is None else c["iou"], rank])


# -- variants ---------------------------------------------------------------

def jpeg(img: Image.Image, width: int | None = None, quality: int = 75) -> bytes:
    if width:
        img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
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


def paint_date(page: Image.Image, band) -> Image.Image:
    img = page.convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle(band, fill="white")
    d.text((band[0] + 2, band[1] + 1), "31.12.2026", fill="black", font_size=max(10, band[3] - band[1] - 4))
    return img


def phone_viewer(page: Image.Image) -> Image.Image:
    phone = Image.new("RGB", (1080, 2340), (0, 0, 0))
    pg = page.convert("RGB").resize((1080, round(page.height * 1080 / page.width)))
    phone.paste(pg, (0, max(0, (2340 - pg.height) // 2)))
    ImageDraw.Draw(phone).text((40, 30), "10:42", fill="white", font_size=40)
    return phone


def phone_photo(page: Image.Image, rng: random.Random) -> Image.Image:
    w, h = page.size
    m = 0.06
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    jitter = [rng.uniform(0, m) for _ in range(8)]
    dst = np.float32([[w * jitter[0], h * jitter[1]], [w * (1 - jitter[2]), h * jitter[3]],
                      [w * (1 - jitter[4]), h * (1 - jitter[5])], [w * jitter[6], h * (1 - jitter[7])]])
    arr = cv2.warpPerspective(np.asarray(page.convert("RGB")), cv2.getPerspectiveTransform(src, dst), (w, h),
                              borderValue=(70, 60, 50))
    yy, xx = np.mgrid[0:h, 0:w]
    light = 0.82 + 0.18 * (xx / w) * (1 - 0.3 * yy / h)
    arr = np.clip(arr * light[..., None], 0, 255).astype(np.uint8)
    return Image.fromarray(arr).filter(ImageFilter.GaussianBlur(0.8))


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


def best_iou(boxes, band, scale) -> float:
    bx0, by0, bx1, by1 = (v * scale for v in band)
    best = 0.0
    for x0, y0, x1, y1 in boxes:
        ix, iy = max(0, min(x1, bx1) - max(x0, bx0)), max(0, min(y1, by1) - max(y0, by0))
        union = (x1 - x0) * (y1 - y0) + (bx1 - bx0) * (by1 - by0) - ix * iy
        best = max(best, ix * iy / union if union else 0.0)
    return best


def nta_pdfs(cache: Path, n: int, seed: int) -> list[Path]:
    """Genuine circulars from another issuer (NTA), downloaded once into ``cache``."""
    cache.mkdir(parents=True, exist_ok=True)
    have = sorted(cache.glob("*.pdf"))
    if len(have) >= n:
        return have[:n]
    f = Fetcher(delay=1.0)
    try:
        page = f.client.get("https://nta.ac.in/").text
        links = sorted({re.sub(r"\s", "%20", h) for h in re.findall(r'href="([^"]+\.pdf)"', page, re.I)})
        links = [h if h.startswith("http") else "https://nta.ac.in/" + h.lstrip("/") for h in links]
        random.Random(seed).shuffle(links)
        store = BlobStore(cache / "_tmp")
        for url in links:
            if len(list(cache.glob("*.pdf"))) >= n:
                break
            out = f.download(url, store)
            if out.status == "ok" and out.kind == "pdf":
                (cache / f"{out.sha256[:16]}.pdf").write_bytes(store.get(out.sha256))
    finally:
        f.close()
    return sorted(cache.glob("*.pdf"))[:n]


# -- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100, help="official files to sample")
    ap.add_argument("--others", type=int, default=40, help="NTA circulars to test (0 to skip)")
    ap.add_argument("--external", type=Path, default=None, help="folder with manifest.csv of outside forgeries")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="write a Markdown summary here")
    ap.add_argument("--json", type=Path, default=None, help="write per-case results here")
    ap.add_argument("--csv", type=Path, default=None, help="write the published test set (one row per case) here")
    args = ap.parse_args()

    root = args.data_dir or default_data_dir()
    db = Database(root / "index.sqlite3")
    store = BlobStore(root)
    rows = db.conn.execute(
        "SELECT sha256, n_pages, text_pages FROM files WHERE kind='pdf' AND index_version > 0 "
        "AND index_error IS NULL AND n_pages > 0 AND sha256 IN ("
        "  SELECT f.sha256 FROM fetches f JOIN links l ON l.url = f.url JOIN notices n ON n.id = l.notice_id"
        "  WHERE f.status = 'ok' AND n.source != 'archive') "  # documents UGC lists today
        "ORDER BY sha256").fetchall()
    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))
    print(f"Sampling {len(sample)} of {len(rows)} indexed PDFs that UGC currently lists", file=sys.stderr)

    verifier = Verifier(db, store, use_ocr=False)
    no_fakes = Verifier(db, store, use_ocr=False, known_fakes=False)
    cases: list[dict] = []
    timings: list[float] = []

    def run(name, data, *, sha=None, scanned=None, band=None, scale=None, filename=None, hide=None, v=verifier,
            note=None):
        t = time.perf_counter()
        res = v.verify(data, filename, exclude=hide)
        timings.append(time.perf_counter() - t)
        localised = iou = None
        if band is not None and res.match is not None:
            boxes = [b for vd in res.match.visual for b in vd.boxes]
            localised = overlaps(boxes, band, scale) if boxes else False
            iou = round(best_iou(boxes, band, scale), 3) if boxes else 0.0
        found = ([res.match.sha256] if res.match else []) + [a["sha256"] for a in res.alternatives]
        cases.append({"variant": name, "sha256": sha, "scanned": scanned, "verdict": res.verdict,
                      "localised": localised, "iou": iou, "note": note, "summary": res.summary,
                      "found": found[:3]})

    for i, row in enumerate(sample, 1):
        sha = row["sha256"]
        data = store.get(sha)
        scanned = (row["text_pages"] or 0) == 0
        try:
            page = render_pdf_page(data, 1, 1000)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {sha[:12]}: {exc}", file=sys.stderr)
            continue
        kw = {"sha": sha, "scanned": scanned}
        run("exact copy", data, **kw)
        run("page 1 saved as a new PDF", first_page_pdf(data), **kw)
        if (np.asarray(page) < 128).mean() > 0.003:
            run("WhatsApp-quality JPEG", jpeg(page, 720, 60), filename="fwd.jpg", **kw)
            run("phone screenshot with black bars", jpeg(phone_viewer(page), None, 75), filename="shot.jpg", **kw)
            run("angled phone photo", jpeg(phone_photo(page, rng), None, 72), filename="photo.jpg", **kw)
            band = text_line_band(page, rng)
            if band:
                edited = paint_date(page, band)
                run("screenshot with a date painted in", jpeg(edited, 1000, 80), band=band, scale=1.2, **kw)
                pdf_edit = pdf_whiteout_edit(data, band, 1000)
                if pdf_edit:
                    run("PDF edited: white-out + typed text", pdf_edit, band=band, scale=1.2, **kw)
                run("phone photo of an edited printout", jpeg(phone_photo(edited, rng), None, 72), **kw)
            run("UGC letterhead, fabricated body", letterhead_fabrication(page), **kw)
        run("genuine UGC notice missing from the index", data, hide={sha}, **kw)
        if i % 10 == 0:
            print(f"  {i}/{len(sample)}", file=sys.stderr)

    # Group 4: debunked fakes, forwarded the ways people forward them.
    fake_shas = sorted({r["sha256"] for r in db.known_fakes()})
    for fsha in fake_shas:
        card = Image.open(io.BytesIO(store.get(fsha))).convert("RGB")
        w, h = card.size
        notice = card.crop((int(w * 0.13), int(h * 0.21), int(w * 0.89), int(h * 0.83)))
        for label, blob in (("re-saved", jpeg(card, None, 80)), ("cropped", jpeg(notice, None, 70)),
                            ("small", jpeg(notice, 420, 55)), ("phone", jpeg(phone_viewer(notice), None, 75))):
            run("fake, forwarded copy", blob, sha=fsha, note=label)
            run("fake, store switched off", blob, sha=fsha, note=label, v=no_fakes)

    # Group 5: another issuer.
    if args.others:
        for path in nta_pdfs(root / "eval" / "nta", args.others, args.seed):
            data = path.read_bytes()
            also_ugc = bool(db.provenance(__import__("hashlib").sha256(data).hexdigest()))
            run("NTA circular", data, note="also published by UGC" if also_ugc else None)

    # Outside forgeries.
    if args.external:
        with (args.external / "manifest.csv").open(encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                name = f"external: {rec.get('expected', '?')}"
                EXPECT.setdefault(name, {rec["expected"]})
                run(name, (args.external / rec["file"]).read_bytes(), filename=rec["file"])

    # -- summary --
    by = defaultdict(list)
    for c in cases:
        by[c["variant"]].append(c)

    correct = is_correct

    def rate(cs) -> str:
        if not cs:
            return "n/a"
        ok = sum(correct(c) for c in cs)
        return f"{ok}/{len(cs)} ({ok / len(cs):.0%})"

    lines = ["| Group | Variant | Expected | Correct | Text-layer PDFs | Scanned PDFs |",
             "|---|---|---|---:|---:|---:|"]
    for group, variants in GROUPS.items():
        for name in variants:
            cs = by.get(name, [])
            if not cs:
                continue
            exp = " or ".join(sorted(EXPECT[name])).replace("VERIFIED_EXACT or VERIFIED_CONTENT", "VERIFIED_*")
            text = [c for c in cs if c["scanned"] is False]
            scan = [c for c in cs if c["scanned"] is True]
            lines.append(f"| {group} | {name} | {exp} | {rate(cs)} | {rate(text)} | {rate(scan)} |")
    for name, cs in by.items():
        if name.startswith("external"):
            lines.append(f"| Outside forgeries | {name} | {name.split(': ')[1]} | {rate(cs)} | | |")

    not_official = [c for c in cases if c["variant"] in NOT_OFFICIAL
                    and not (c["variant"] == "NTA circular" and c["note"] == "also published by UGC")]
    false_verified = [c for c in not_official if c["verdict"] in VERIFIED]
    altered = [c for c in cases if c["variant"] in GROUPS["3. Altered"]]
    flagged = [c for c in altered if c["verdict"] in (ALTERED, KNOWN_FAKE)]
    altered_unconfirmed = sum(c["verdict"] == UNCONFIRMED_MATCH for c in altered)
    edits = [c for c in altered if c["localised"] is not None and c["verdict"] == ALTERED]
    loc = sum(bool(c["localised"]) for c in edits)
    cov = db.coverage()
    held = by.get("genuine UGC notice missing from the index", [])
    held_counts = Counter(c["verdict"] for c in held)
    nta_also = sum(1 for c in by.get("NTA circular", []) if c["note"] == "also published by UGC")

    # Detection as a yes/no question: "not verified" against "not an official UGC document".
    scored = [c for c in cases if c["variant"] in EXPECT and not c["variant"].startswith("external")
              and c["variant"] != "genuine UGC notice missing from the index"
              and not (c["variant"] == "NTA circular" and c["note"] == "also published by UGC")]
    positive = [c for c in scored if c["variant"] in NOT_OFFICIAL]
    negative = [c for c in scored if c["variant"] not in NOT_OFFICIAL]
    tp = sum(c["verdict"] not in VERIFIED for c in positive)
    fp = sum(c["verdict"] not in VERIFIED for c in negative)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(len(positive), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    genuine_forwards = [c for c in cases if c["variant"] in GROUPS["2. Forwarded"]]
    unconfirmed = sum(c["verdict"] == UNCONFIRMED_MATCH for c in genuine_forwards)
    retrieval = [c for c in cases if c["variant"] in (*GROUPS["1. Published copy"], *GROUPS["2. Forwarded"])
                 and c["sha256"]]
    at1 = sum(bool(c["found"]) and c["found"][0] == c["sha256"] for c in retrieval)
    at3 = sum(c["sha256"] in c["found"] for c in retrieval)
    ious = [c["iou"] for c in edits if c["localised"]]

    out = ["## Evaluation", "",
           f"{len(sample)} official UGC PDFs sampled at random (seed {args.seed}) from the {len(rows):,} "
           "indexed PDFs UGC currently lists, "
           f"{len(fake_shas)} debunked fakes from PIB Fact Check, and {len(by.get('NTA circular', []))} genuine "
           "NTA circulars. No OCR. Every case is checked against the full index.", "",
           "| Headline | Result |", "|---|---|",
           f"| False verified: not an official UGC document, but called Verified | "
           f"**{len(false_verified)} of {len(not_official)}** ({len(false_verified) / max(len(not_official), 1):.1%}) |",
           f"| Altered notices caught (Altered or Known fake) | {len(flagged)} of {len(altered)} "
           f"({len(flagged) / max(len(altered), 1):.0%}); {altered_unconfirmed} more matched with details "
           "unconfirmed, none verified |",
           f"| Edits caught with a marked region on the edit | {loc} of {len(edits)} "
           f"(mean IoU {statistics.mean(ious) if ious else 0:.2f}) |",
           f"| Flagging \"not verified\" against \"not official\": precision / recall / F1 | "
           f"{precision:.3f} / {recall:.3f} / {f1:.3f} |",
           f"| Genuine copies not verified (false-positive rate) | {fp} of {len(negative)} "
           f"({fp / max(len(negative), 1):.1%}), of which {unconfirmed} matched but with details unconfirmed |",
           f"| Original found among the top 1 / top 3 candidates (published and forwarded copies) | "
           f"{at1 / max(len(retrieval), 1):.1%} / {at3 / max(len(retrieval), 1):.1%} |",
           f"| Index coverage of files UGC lists | {cov['coverage_pct']}% |",
           f"| Median time per check | {statistics.median(timings):.2f} s "
           f"(90th percentile {sorted(timings)[int(len(timings) * 0.9)]:.2f} s) |", "",
           *lines, ""]
    if nta_also:
        out += [f"{nta_also} of the NTA circulars are also published by UGC (same file), so Verified is correct "
                "for those.", ""]
    out += ["**Genuine notices missing from the index** (what a stale index sees): "
            + ", ".join(f"{v} {n}" for v, n in held_counts.most_common()) + ". "
            "Altered here means a close relative exists, usually the same letter issued for another year; the "
            "report shows the differences and suggests refreshing the index.", ""]
    report = "\n".join(out)
    print(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(cases, indent=1), encoding="utf-8")
    if args.csv:
        write_cases_csv(cases, args.csv, args.seed)
    for c in false_verified[:20]:
        print(f"FALSE VERIFIED {c['variant']}: {c['sha256'] and c['sha256'][:12]} -> {c['summary'][:140]}",
              file=sys.stderr)
    misses = [c for c in cases if c["variant"] in EXPECT and not correct(c)]
    for c in misses[:30]:
        print(f"MISS {c['variant']}: {(c['sha256'] or '')[:12]} -> {c['verdict']}: {c['summary'][:120]}",
              file=sys.stderr)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
