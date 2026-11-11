from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageChops, ImageDraw

from conftest import DEADLINE_LINE
from pdfgen import line_box

from circverify.diff import _lines_to_check, classify, compare_images, compare_text


def test_identical_text_has_no_changes():
    r = compare_text("The last date is 15 November.", "The last date is 15 November.")
    assert r.ratio == 1.0 and r.changes == []


def test_split_words_and_case_are_cosmetic():
    r = compare_text("The applica tion FEE is waived.", "The application fee is waived")
    assert r.changes == [] and r.cosmetic == 1


def test_changes_carry_context_and_reasons():
    r = compare_text("Apply at www.ugc.gov.in before 15 November 2026 without any fee.",
                     "Apply at www.ugc-help.in before 15 November 2026 with a fee of Rs 500.")
    kinds = [set(c.reasons) for c in r.changes]
    assert any("link or contact" in k for k in kinds)
    assert any("money" in k for k in kinds)
    assert all(c.severity == "high" for c in r.changes)


def test_plain_wording_change_is_normal_severity():
    severity, reasons = classify("give wide publicity", "share widely")
    assert severity == "normal" and reasons == []


def test_negation_is_high_severity():
    severity, reasons = classify("is recognised", "is not recognised")
    assert severity == "high" and "meaning-changing word" in reasons


def test_identical_and_slightly_shifted_images_have_no_regions(official_render):
    same = compare_images(official_render, official_render)
    assert same.boxes == [] and same.changed_ratio == 0
    shifted = ImageChops.offset(official_render, 3, -2)
    r = compare_images(official_render, shifted)
    assert r.boxes == [] and r.same_document


def test_edit_is_boxed_where_it_happened(official_render):
    img = official_render.copy()
    box = line_box(DEADLINE_LINE, img.width)
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=255)
    d.text((box[0] + 4, box[1] + 2), "of applications to 31 January 2027.", fill=0, font_size=20)
    r = compare_images(official_render, img)
    assert r.same_document and r.boxes
    s = 1200 / img.width
    assert all(box[1] * s - 30 <= y0 and y1 <= box[3] * s + 30 for _, y0, _, y1 in r.boxes)


def test_a_different_page_is_not_the_same_document(official_render):
    from conftest import OTHER
    from pdfgen import make_pdf
    from circverify.extract import render_pdf_page

    other = render_pdf_page(make_pdf([OTHER]), 1, 1000)
    assert not compare_images(official_render, other).same_document


def _text_grid():
    """A page of cells: low noise everywhere, text on two rows out of three, light ink density."""
    rng = np.random.default_rng(0)
    diff = rng.uniform(0, 8, (40, 60)).astype(np.float32)
    considered = np.zeros(diff.shape, dtype=bool)
    considered[0::3, 5:55] = considered[1::3, 5:55] = True
    return diff, considered, np.full(diff.shape, 0.12)


def test_a_line_that_differs_a_little_all_along_is_one_to_check():
    diff, considered, density = _text_grid()
    assert _lines_to_check(diff, considered, density, 20.0) == []
    diff[19, 10:40] = 22.0  # just over the threshold, cell after cell
    (x0, y0, x1, y1), = _lines_to_check(diff, considered, density, 20.0)
    assert 8 <= x0 <= 12 and 38 <= x1 <= 42 and y0 <= 19 < y1


def test_lines_to_check_skip_logos_quiet_lines_and_clean_page_noise():
    diff, considered, density = _text_grid()
    diff[19, 10:40] = 22.0
    dense = density.copy()
    dense[18:22, :] = 0.5  # an emblem or a heading rule, not body text
    assert _lines_to_check(diff, considered, dense, 20.0) == []
    assert _lines_to_check(diff, considered, density, 30.0) == []  # under the page's own threshold
    diff[19, 10:40] = 12.0
    assert _lines_to_check(diff, considered, density, 10.0) == []  # a very clean page: below the floor


def test_genuine_recompressed_copies_have_no_lines_to_check(official_render):
    for width, quality in ((None, 80), (720, 60)):
        img = official_render.convert("RGB")
        if width:
            img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        r = compare_images(official_render, Image.open(io.BytesIO(buf.getvalue())))
        assert r.same_document and not r.boxes and r.lines_to_check == []
