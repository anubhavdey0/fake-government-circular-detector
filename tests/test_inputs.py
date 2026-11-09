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
