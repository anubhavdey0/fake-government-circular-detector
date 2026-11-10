"""The evidence checklist behind a verdict, and links to later documents that update a circular."""

from __future__ import annotations

from conftest import DEADLINE_LINE, OFFICIAL, add_official
from pdfgen import make_pdf

from circverify.htmlreport import render_result
from circverify.indexer import index_pending
from circverify.matcher import ALTERED, NO_MATCH, VERIFIED_EXACT, Verifier
from circverify.relations import build, classify

CORRIGENDUM = [
    "University Grants Commission",
    "Bahadur Shah Zafar Marg, New Delhi - 110002",
    "F.No. 1-15/2026 (CPP-III)                              6 November 2026",
    "CORRIGENDUM",
    "In partial modification of this office letter F.No. 1-15/2026 (CPP-II) dated",
    "30 October 2026, the last date for online submission of applications under",
    "the fellowship scheme shall be read as 30 November 2026 instead of 15 November 2026.",
    "All other terms and conditions remain unchanged.",
    "(Secretary)",
]


def _check(res, name):
    return next((r for r in res.evidence if r["check"] == name), None)


def test_exact_copy_evidence(index, official_pdf):
    db, store, _ = index
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert res.verdict == VERIFIED_EXACT
    assert _check(res, "Exact file match")["status"] == "ok"
    assert _check(res, "Official original")["status"] == "ok"
    assert _check(res, "Still current")["value"] == "Listed on ugc.gov.in"
    assert "Evidence at a glance" in render_result(res)


def test_altered_copy_evidence_names_the_critical_change(index):
    db, store, _ = index
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    res = Verifier(db, store, use_ocr=False).verify(make_pdf([lines]), "forged.pdf")
    assert res.verdict == ALTERED
    text = _check(res, "Text")
    assert text["status"] == "bad" and "dates, numbers" in text["value"]
    assert _check(res, "Reference number")["status"] == "ok"
    assert _check(res, "Issue date")["status"] == "ok"
    assert all(r["check"] != "Exact file match" or r["status"] == "neutral" for r in res.evidence)


def test_not_found_evidence_flags_a_borrowed_reference_number(index):
    db, store, _ = index
    fake = make_pdf([[*OFFICIAL[:3], "Subject: Offline examinations in all universities",
                      "All universities are hereby advised to conduct the coming semester",
                      "examinations in offline mode at the home centres of the students.", "(Secretary)"]])
    res = Verifier(db, store, use_ocr=False).verify(fake)
    assert res.verdict == NO_MATCH
    ref = _check(res, "Reference number")
    assert ref["status"] == "warn" and "belongs to a different official document" in ref["value"]


def test_corrigendum_is_linked_to_the_letter_it_corrects(index, official_pdf):
    db, store, _ = index
    add_official(db, store, make_pdf([CORRIGENDUM]), "Corrigendum: fellowship applications", "1003", "2026-11-06")
    index_pending(db, store, workers=1, use_ocr=False)
    counts = build(db)
    assert counts.get("corrects") == 1
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert res.verdict == VERIFIED_EXACT  # still genuine...
    [later] = res.updated_by  # ...but no longer the whole story
    assert later["kind"] == "corrects" and later["title"].startswith("Corrigendum")
    assert "partial modification" in later["evidence"]
    assert _check(res, "Still current")["status"] == "warn"
    assert "A later document may update this one" in render_result(res)
    corr = Verifier(db, store, use_ocr=False).verify(make_pdf([CORRIGENDUM]))
    assert [d["sha256"] for d in corr.updates] == [index[2]]  # and the corrigendum points back to it


def test_title_based_extension_link(tmp_path):
    from circverify.db import Database
    from circverify.store import BlobStore

    db, store = Database(tmp_path / "i.sqlite3"), BlobStore(tmp_path)
    body = ["University Grants Commission", "Subject: National fellowship for research scholars",
            "Applications are invited from eligible research scholars for the national fellowship",
            "through the online portal. The last date is 15 November 2026."]
    add_official(db, store, make_pdf([body]), "Applications for the National Fellowship for research scholars",
                 "2001", "2026-09-01")
    ext = ["University Grants Commission", "Subject: Extension of the last date",
           "The last date for the national fellowship for research scholars is now 30 November 2026."]
    add_official(db, store, make_pdf([ext]), "Extension of last date: National Fellowship for research scholars",
                 "2002", "2026-11-10")
    index_pending(db, store, workers=1, use_ocr=False)
    assert build(db) == {"extends": 1}
    first = db.conn.execute("SELECT to_sha FROM relations").fetchone()[0]
    assert db.later_documents(first)[0]["kind"] == "extends"
    db.close()


def test_relation_words():
    assert classify("in supersession of the earlier notice") == "supersedes"
    assert classify("the letter stands withdrawn with immediate effect") == "withdraws"
    assert classify("the last date has been extended to 30 June") == "extends"
    assert classify("in continuation of this office letter") == "follows"
    assert classify("extension activities in rural areas") is None


def test_key_dates_come_from_the_official_text(index, official_pdf):
    from datetime import date

    from circverify.deadlines import describe_when, key_dates

    db, store, _ = index
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    [deadline] = [d for d in res.key_dates if d["kind"] == "deadline"]
    assert deadline["date"] == "2026-11-15" and "last date" in deadline["context"]
    assert "Dates in the official document" in render_result(res)
    assert describe_when("2026-11-15", date(2026, 11, 20)) == "passed"
    assert describe_when("2026-11-15", date(2026, 11, 10)) == "in 5 days"
    assert key_dates("This notice was earlier issued on 1 October 2026.") == []


def test_details_compared_and_same_text_elsewhere(index, official_pdf):
    db, store, sha = index
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    res = Verifier(db, store, use_ocr=False).verify(make_pdf([lines], producer="Canva"), "forged.pdf")
    html = render_result(res)
    assert "Details compared" in html and "Canva" in html and "Office scanner suite" in html
    # The same circular re-posted under another entry is listed as the same text.
    add_official(db, store, make_pdf([OFFICIAL], producer="Re-saved"), "Fellowship applications (re-posted)", "1009")
    index_pending(db, store, workers=1, use_ocr=False)
    again = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert [d["title"] for d in again.duplicates] == ["Fellowship applications (re-posted)"]
    assert "The same text was also published as" in render_result(again)


def _angled_photo(img):
    import cv2
    import numpy as np

    from conftest import png_bytes

    rgb = np.asarray(img.convert("RGB"))
    h, w = rgb.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[w * 0.05, h * 0.03], [w * 0.97, h * 0.01], [w * 0.94, h * 0.97], [w * 0.02, h * 0.95]])
    warped = cv2.warpPerspective(rgb, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(70, 60, 50))
    from PIL import Image

    return png_bytes(Image.fromarray(warped), "JPEG", quality=80)


def test_photo_matches_but_details_need_ocr(index, official_render):
    from circverify.matcher import UNCONFIRMED_MATCH, VERIFIED_CONTENT

    db, store, _ = index
    photo = _angled_photo(official_render)
    res = Verifier(db, store, use_ocr=False).verify(photo, "photo.jpg")
    assert res.verdict == UNCONFIRMED_MATCH
    assert "cannot rule out a small edit" in res.summary and res.match is not None
    assert _check(res, "Page image")["status"] == "warn"
    # Reading the words confirms the details...
    from circverify.ocr import OcrText

    read = Verifier(db, store, ocr=lambda img: OcrText("\n".join(OFFICIAL), 0.86)).verify(photo, "photo.jpg")
    assert read.verdict == VERIFIED_CONTENT and read.ocr_confidence == 0.86
    assert "OCR (mean confidence 86%)" in render_result(read)
    # ...or shows what OCR read differently, without calling recognition errors an alteration.
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    odd = Verifier(db, store, ocr=lambda img: "\n".join(lines)).verify(photo, "photo.jpg")
    assert odd.verdict == UNCONFIRMED_MATCH and odd.match.text.changes
