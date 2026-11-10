"""End-to-end: index a made-up circular, then submit genuine and doctored copies of it."""

from __future__ import annotations

from PIL import ImageDraw

from conftest import DEADLINE_LINE, OFFICIAL, UNRELATED, png_bytes
from pdfgen import line_box, make_pdf

from circverify.matcher import ALTERED, NO_MATCH, UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT, Verifier


def verifier(index) -> Verifier:
    db, store, _ = index
    return Verifier(db, store, use_ocr=False)


def with_line(lines, i, new):
    out = list(lines)
    out[i] = new
    return out


def test_exact_copy_is_verified_with_provenance(index, official_pdf):
    res = verifier(index).verify(official_pdf, "copy.pdf")
    assert res.verdict == VERIFIED_EXACT
    assert res.match.primary.title == "Extension of last date for fellowship applications"
    assert res.match.primary.url.endswith("/pdfnews/1001.pdf")


def test_resaved_copy_with_same_text_is_verified_content(index):
    resaved = make_pdf([OFFICIAL], producer="Microsoft: Print To PDF")
    res = verifier(index).verify(resaved)
    assert res.verdict == VERIFIED_CONTENT
    assert not res.match.text.changes


def test_changed_deadline_is_reported_as_high_severity(index):
    forged = make_pdf([with_line(OFFICIAL, DEADLINE_LINE,
                                 "of applications under the fellowship scheme to 25 November 2026.")])
    res = verifier(index).verify(forged)
    assert res.verdict == ALTERED
    [change] = res.match.text.changes
    assert (change.official, change.submitted) == ("15", "25")
    assert change.severity == "high" and "number" in change.reasons
    # The page image differs only around the edited line.
    [page] = res.match.visual
    assert page.same_document and page.boxes


def test_swapped_link_is_flagged(index):
    forged = make_pdf([with_line(OFFICIAL, 10, "www.ugc-fellowship.in. A processing fee of Rs 500 is payable.")])
    res = verifier(index).verify(forged)
    assert res.verdict == ALTERED
    reasons = {r for c in res.match.text.changes for r in c.reasons}
    assert {"link or contact", "money"} <= reasons


def test_extra_page_is_flagged(index):
    forged = make_pdf([OFFICIAL, ["Annexure: bank account for fee payment", "Account 001234567890",
                                  "IFSC SBIN0000001, payable before 20 November 2026"]])
    res = verifier(index).verify(forged)
    assert res.verdict == ALTERED
    assert res.match.unmatched_pages == [2]


def test_unrelated_document_is_not_found(index):
    res = verifier(index).verify(make_pdf([UNRELATED]))
    assert res.verdict == NO_MATCH
    assert res.match is None


def test_letterhead_alone_is_not_a_match(index):
    fake = make_pdf([OFFICIAL[:3] + [
        "Subject: Scholarship of Rs 50,000 for all students",
        "All students who register at www.ugc-scholarship.online before Friday",
        "will receive a one-time scholarship directly in their bank account.",
        "A registration fee of Rs 199 must be paid through the same website.",
    ]])
    res = verifier(index).verify(fake)
    assert res.verdict == NO_MATCH


def test_whatsapp_style_screenshot_is_verified_by_page_image(index, official_render):
    small = official_render.resize((700, round(official_render.height * 0.7)))
    jpeg = png_bytes(small.convert("RGB"), "JPEG", quality=70)
    res = verifier(index).verify(jpeg, "forward.jpg")
    assert res.text_source == "none"  # no OCR in this test
    assert res.verdict == VERIFIED_CONTENT
    assert res.match.visual[0].same_document and not res.match.visual[0].boxes


def test_cropped_screenshot_is_found_by_ocr_text_and_lined_up(index, official_render):
    # A page-image hash describes the whole page, so a crop is found through
    # its text. This stub stands in for Tesseract reading the visible lines.
    top = official_render.crop((0, 0, official_render.width, int(official_render.height * 0.30)))
    db, store, _ = index
    v = Verifier(db, store, ocr=lambda img: "\n".join(OFFICIAL[:10]))
    res = v.verify(png_bytes(top), "crop.png")
    assert res.text_source == "ocr"
    assert res.verdict == VERIFIED_CONTENT
    page = res.match.visual[0]
    assert page.same_document and not page.boxes and page.coverage < 0.4


def test_cropped_screenshot_without_ocr_is_not_found(index, official_render):
    top = official_render.crop((0, 0, official_render.width, int(official_render.height * 0.30)))
    res = verifier(index).verify(png_bytes(top), "crop.png")
    assert res.verdict == NO_MATCH
    assert any("no OCR engine" in n for n in res.notes)


def test_photoshopped_screenshot_is_altered_and_localised(index, official_render):
    img = official_render.convert("RGB")
    box = line_box(DEADLINE_LINE, img.width)
    draw = ImageDraw.Draw(img)
    draw.rectangle(box, fill="white")
    draw.text((box[0] + 4, box[1] + 2), "of applications under the scheme to 30 December 2026.", fill="black",
              font_size=int(12 * img.width / 612))
    res = verifier(index).verify(png_bytes(img), "edited.png")
    assert res.verdict == ALTERED
    page = res.match.visual[0]
    assert page.boxes
    # Every changed region sits on the edited line (coordinates are at the 1200 px diff width).
    s = 1200 / img.width
    lo, hi = box[1] * s - 30, box[3] * s + 30
    assert all(lo <= y0 and y1 <= hi for _, y0, _, y1 in page.boxes)


def test_whiteout_over_unchanged_text_layer_is_altered(index):
    # The text layer still says "15 November 2026", but a white box hides the
    # date on the page. Text comparison alone would call this genuine.
    baseline = 760 - DEADLINE_LINE * 18
    forged = make_pdf([OFFICIAL], white_boxes={0: [(330, baseline - 4, 120, 16)]})
    res = verifier(index).verify(forged)
    assert not res.match.text.changes
    assert res.verdict == ALTERED
    assert any("pasted over" in n for n in res.notes)


def test_garbage_is_unreadable(index):
    res = verifier(index).verify(b"hello, this is not a document")
    assert res.verdict == UNREADABLE


def test_exclusion_hides_a_document(index, official_pdf):
    db, store, sha = index
    res = Verifier(db, store, use_ocr=False, exclude={sha}).verify(official_pdf)
    assert res.verdict == NO_MATCH


def test_per_call_exclusion_leaves_the_verifier_intact(index, official_pdf):
    db, store, sha = index
    v = verifier(index)
    assert v.verify(official_pdf, exclude={sha}).verdict == NO_MATCH
    assert v.verify(official_pdf).verdict == VERIFIED_EXACT


def test_json_round_trip(index):
    import json

    forged = make_pdf([with_line(OFFICIAL, DEADLINE_LINE,
                                 "of applications under the fellowship scheme to 25 November 2026.")])
    d = verifier(index).verify(forged).to_dict()
    json.dumps(d)
    assert d["verdict"] == ALTERED and d["match"]["text_changes"][0]["submitted"] == "25"
