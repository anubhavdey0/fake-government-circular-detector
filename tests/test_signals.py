"""Hints for unverified documents: links and QR codes, image metadata, editing tools, page layout.

None of these may change a verdict; they only add notes under "Signals, not a verdict".
"""

from __future__ import annotations


import numpy as np
import pytest
from PIL import Image, ImageDraw

from conftest import png_bytes
from pdfgen import make_pdf

from circverify.forensics import compare_signals, image_notes, image_signals, tool_family
from circverify.layout import FEATURES, LayoutModel, describe, layout_features
from circverify.links import assess, check, describe as describe_link, find_in_text, registrable
from circverify.matcher import NO_MATCH, VERIFIED_EXACT, Verifier

KNOWN = {"ugcnetonline.in": 22, "gmail.com": 383, "ugc-net-warning.com": 3}


@pytest.mark.parametrize("host, kind", [
    ("www.ugc.gov.in", "official"), ("deb.ugc.ac.in", "official"), ("scholarships.gov.in", "government"),
    ("mail.nic.in", "government"), ("bit.ly", "shortener"), ("ugc-gov-in.com", "lookalike"),
    ("ugcindia.org", "lookalike"), ("ugc.gov.in.verify-now.com", "lookalike"), ("ugcgov.in", "lookalike"),
    ("xn--ugc-8na.in", "lookalike"), ("ugcnetonline.in", "known"), ("gmail.com", "known"),
    ("randomsite.com", "unknown"),
    # Mentioned in a few official documents (say, a warning about it) but still a lookalike.
    ("ugc-net-warning.com", "lookalike"),
])
def test_domain_groups(host, kind):
    assert assess(host, KNOWN)[0] == kind


def test_registrable_domains():
    assert registrable("uamp.ugc.ac.in") == "ugc.ac.in"
    assert registrable("www.ugc-gov-in.com") == "ugc-gov-in.com"
    assert registrable("ugc.gov.in.verify-now.com") == "verify-now.com"


def test_links_addresses_and_bare_domains_are_found():
    found = find_in_text("Pay at https://ugc-gov-in.com/fee or www.ugc.gov.in; mail ugc.fees@gmail.com; "
                         "see bit.ly/x9 and nta.ac.in.")
    assert ("text", "https://ugc-gov-in.com/fee") in found
    assert ("email", "ugc.fees@gmail.com") in found
    assert ("text", "bit.ly/x9") in found and ("text", "nta.ac.in") in found


def _page_with_qr(payload: str) -> Image.Image:
    import qrcode

    page = Image.new("L", (1000, 1400), 255)
    ImageDraw.Draw(page).text((80, 80), "University Grants Commission", fill=0, font_size=36)
    page.paste(qrcode.make(payload).convert("L").resize((260, 260)), (660, 1060))
    return page


def test_qr_code_to_a_lookalike_site_is_reported():
    [f] = check("", [(1, _page_with_qr("https://ugc-gov-in.com/register"))], KNOWN)
    assert (f.source, f.kind, f.imitates, f.page) == ("qr", "lookalike", "ugc.gov.in", 1)
    assert "imitates ugc.gov.in" in describe_link(f)


def test_qr_code_with_plain_text_is_noted_not_judged():
    [f] = check("", [(1, _page_with_qr("Verified by UGC"))], KNOWN)
    assert f.kind == "not-a-link" and "holds text rather than a link" in describe_link(f)


def test_unverified_document_gets_link_hints_but_same_verdict(index):
    db, store, _ = index
    fake = make_pdf([["University Grants Commission", "Subject: Special registration drive",
                      "All students must pay a registration fee of Rs 999 at",
                      "https://ugc-gov-in.com/pay before 30 November 2026 to keep their degree valid."]])
    res = Verifier(db, store, use_ocr=False).verify(fake, "fake.pdf")
    assert res.verdict == NO_MATCH
    assert any("imitates ugc.gov.in" in n for n in res.signal_notes)
    assert any(link["kind"] == "lookalike" for link in res.links)


def test_verified_document_is_not_scanned_for_links(index, official_pdf):
    db, store, _ = index
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert res.verdict == VERIFIED_EXACT and res.links == []


def test_image_saved_by_an_editor_is_noted():
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.1 (Windows)"
    data = png_bytes(Image.new("RGB", (400, 300), "white"), "JPEG", exif=exif)
    sig = image_signals(data)
    assert sig["software"].startswith("Adobe Photoshop")
    assert image_notes(sig) and "image editor" in image_notes(sig)[0]
    assert image_notes(image_signals(png_bytes(Image.new("RGB", (40, 30), "white"), "JPEG"))) == []


def test_editing_tool_is_weighed_against_what_the_issuer_uses():
    usage, total = {"ilovepdf": 101, "acrobat distiller": 577}, 6704
    [own] = compare_signals({"producer": "iLovePDF"}, None, usage, total)
    assert "means little" in own and "101" in own
    [canva] = compare_signals({"producer": "Canva"}, None, usage, total)
    assert "none of UGC's 6,704 own PDFs" in canva
    assert tool_family("Microsoft® Word 2016") == "microsoft word"
    assert tool_family("doPDF Ver 7.3 Build 382") == "dopdf"


def test_layout_features_measure_a_page(official_pdf):
    from circverify.extract import render_pdf_page

    feats = layout_features(render_pdf_page(official_pdf, 1, 800))
    assert feats is not None and len(feats) == len(FEATURES)
    assert 1.2 < feats[0] < 1.5  # A4-ish
    assert layout_features(Image.new("L", (800, 1100), 255)) is None


def test_layout_model_flags_only_outliers():
    rng = np.random.default_rng(3)
    base = np.array([1.41, 0.1, 0.1, 0.06, 0.1, 0.05, 0.08, 0.01, 4.0, 0.012, 0.8, 0.2])
    real = base + rng.normal(0, 0.01, size=(400, len(base))) * np.maximum(base, 0.05)
    model = LayoutModel(real)
    typical = model.score(list(base))
    assert not typical.unusual and describe(typical) is None
    odd = base.copy()
    odd[3], odd[6] = 0.45, 0.0  # text starts halfway down, no letterhead
    score = model.score(list(odd))
    assert score.unusual and "top margin" in describe(score)


def test_hint_text_never_says_fake():
    for note in (describe_link(check("http://ugc-gov-in.com", [], KNOWN)[0]),
                 describe(LayoutModel(np.random.default_rng(1).normal(1, 0.01, (100, len(FEATURES))))
                          .score([9.0] * len(FEATURES)))):
        assert note and "fake" not in note.lower()
