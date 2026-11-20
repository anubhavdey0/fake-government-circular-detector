"""Awkward real-world inputs: odd image modes, phone screenshots, rotations, damaged files."""

from __future__ import annotations

import io

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from conftest import OFFICIAL, png_bytes
from pdfgen import make_pdf

from circverify.matcher import UNREADABLE, VERIFIED_CONTENT, Verifier


def check(index, data: bytes, name: str = "x"):
    db, store, _ = index
    return Verifier(db, store, use_ocr=False).verify(data, name)


def test_16_bit_grayscale_png(index, official_render):
    deep = Image.fromarray(np.asarray(official_render).astype(np.uint16) * 257)
    assert check(index, png_bytes(deep)).verdict == VERIFIED_CONTENT


def test_transparent_background_is_treated_as_white(index, official_render):
    # Transparent pixels stored as (0, 0, 0, 0) must not turn the paper black.
    rgba = np.asarray(official_render.convert("RGBA")).copy()
    paper = (rgba[..., :3] > 245).all(-1)
    rgba[paper] = (0, 0, 0, 0)
    assert check(index, png_bytes(Image.fromarray(rgba, "RGBA"))).verdict == VERIFIED_CONTENT


def test_phone_screenshot_with_black_bars_and_status_bar(index, official_render):
    page = official_render.convert("RGB").resize((1080, round(official_render.height * 1080 / official_render.width)))
    phone = Image.new("RGB", (1080, 2340), (0, 0, 0))
    phone.paste(page, (0, (2340 - page.height) // 2))
    phone.paste((255, 255, 255), (40, 30, 160, 70))  # a clock in the status bar
    res = check(index, png_bytes(phone, "JPEG", quality=80), "screenshot.jpg")
    assert res.verdict == VERIFIED_CONTENT
    assert any("found inside" in n for n in res.notes)


def test_sideways_picture_is_turned_upright(index, official_render):
    res = check(index, png_bytes(official_render.rotate(90, expand=True)), "sideways.png")
    assert res.verdict == VERIFIED_CONTENT
    assert any("rotated" in n for n in res.notes)


def test_pdf_page_with_rotate_flag_has_identical_text(index):
    src = pdfium.PdfDocument(make_pdf([OFFICIAL]))
    out = pdfium.PdfDocument.new()
    out.import_pages(src, [0])
    out[0].set_rotation(90)
    buf = io.BytesIO()
    out.save(buf)
    res = check(index, buf.getvalue())
    assert res.verdict == VERIFIED_CONTENT
    assert not res.match.text.changes


def test_decompression_bomb_is_refused(index):
    res = check(index, png_bytes(Image.new("1", (9000, 9000), 1)))
    assert res.verdict == UNREADABLE and "too large" in res.summary


def test_damaged_and_empty_pdfs_explain_themselves(index):
    pdf = make_pdf([OFFICIAL])
    assert "damaged" in check(index, pdf[: len(pdf) // 3]).summary
    assert check(index, b"").verdict == UNREADABLE


def test_bilingual_ocr_keeps_hindi_lines_and_the_surer_english_reading():
    from circverify.ocr import merge_lines

    head = [[0, 0], [100, 0], [100, 10], [0, 10]]
    line = [[0, 20], [100, 20], [100, 30], [0, 30]]
    en = ("en", [(head, "fen FET STT", 0.51), (line, "University Grants Commission", 0.99)])
    hi = ("hi", [(head, "\u0935\u093f\u0936\u094d\u0935", 0.94), (line, "Univrsity Grants Commission", 0.90)])
    text, conf = merge_lines([en, hi])
    assert text.splitlines() == ["\u0935\u093f\u0936\u094d\u0935", "University Grants Commission"]
    assert round(conf, 3) == round((0.94 + 0.99) / 2, 3)
    assert merge_lines([]) == ("", None)


def test_redo_ocr_reads_pages_again_but_keeps_a_much_longer_first_reading(index, monkeypatch):
    from circverify import indexer
    from circverify.ocr import OcrText

    db, store, sha = index
    first = "University Grants Commission fellowship notice read the first time in English only " * 3
    db.conn.execute("UPDATE pages SET text=?, text_source='ocr' WHERE sha256=? AND page_no=1", (first, sha))
    db.commit()
    reading = {"text": "विश्वविद्यालय अनुदान आयोग " + first}
    monkeypatch.setattr(indexer, "load_ocr", lambda *a, **k: (lambda img: OcrText(reading["text"], 0.9)))
    page = lambda: db.conn.execute("SELECT text FROM pages WHERE sha256=? AND page_no=1", (sha,)).fetchone()[0]

    indexer.enrich_pending(db, store, workers=1, use_ocr=True)  # already OCR'd: left alone
    assert page() == first
    indexer.enrich_pending(db, store, workers=1, use_ocr=True, redo_ocr=True)
    assert page().startswith("विश्वविद्यालय")
    reading["text"] = "x"  # a failed second reading does not replace a good first one
    indexer.enrich_pending(db, store, workers=1, use_ocr=True, redo_ocr=True)
    assert page().startswith("विश्वविद्यालय")
