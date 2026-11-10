"""Known fakes, pasted text, withdrawals, signals, receipts, feeds and the Telegram bot."""

from __future__ import annotations

import io
import json
import re

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from conftest import DEADLINE_LINE, OFFICIAL, png_bytes
from pdfgen import make_pdf

from circverify.extract import render_pdf_page
from circverify.fakes import extract_document, parse_telegram_posts, register_fake
from circverify.matcher import (
    ALTERED,
    KNOWN_FAKE,
    NO_MATCH,
    UNREADABLE,
    VERIFIED_CONTENT,
    VERIFIED_EXACT,
    Verifier,
)

FAKE_LINES = OFFICIAL[:3] + [
    "(Public Notice)",
    "Subject: Offline examinations in all universities for all semesters",
    "All universities are hereby advised to conduct the coming semester",
    "examinations in offline mode at the home centres of the students.",
    "Universities are requested to take prior preparation for the same.",
    "(Secretary)",
]


@pytest.fixture()
def fake_image() -> Image.Image:
    return render_pdf_page(make_pdf([FAKE_LINES]), 1, 1000)


@pytest.fixture()
def with_fake(index, fake_image):
    db, store, sha = index
    added, status = register_fake(db, store, png_bytes(fake_image), source="PIB Fact Check",
                                  url="https://t.me/PIB_FactCheck/999", published="2021-12-13",
                                  claim="Fake notice on offline examinations", verifier=Verifier(db, store, use_ocr=False))
    assert added, status
    return db, store, sha


def test_official_documents_cannot_be_registered_as_fakes(index, official_render):
    db, store, _ = index
    sha, status = register_fake(db, store, png_bytes(official_render), source="PIB Fact Check",
                                url="https://t.me/x/1", published=None, claim=None,
                                verifier=Verifier(db, store, use_ocr=False))
    assert sha is None and status.startswith("refused")


def test_forwarded_copies_of_a_debunked_fake(with_fake, fake_image):
    db, store, _ = with_fake
    v = Verifier(db, store, use_ocr=False)
    for data in (png_bytes(fake_image.convert("RGB"), "JPEG", quality=60),
                 png_bytes(fake_image.resize((600, round(fake_image.height * 0.6))))):
        res = v.verify(data, "forward.jpg")
        assert res.verdict == KNOWN_FAKE
        assert res.known_fake["debunks"][0]["url"] == "https://t.me/PIB_FactCheck/999"
        assert "PIB Fact Check" in res.summary


def test_genuine_documents_are_never_called_fake(with_fake, official_pdf, official_render):
    db, store, _ = with_fake
    v = Verifier(db, store, use_ocr=False)
    assert v.verify(official_pdf).verdict == VERIFIED_EXACT
    assert v.verify(png_bytes(official_render)).verdict == VERIFIED_CONTENT


def test_telegram_preview_parsing():
    page = ('<div class="tgme_widget_message" data-post="PIB_FactCheck/125">'
            '<a class="tgme_widget_message_photo_wrap 1 2" href="https://t.me/PIB_FactCheck/125" '
            "style=\"width:641px;background-image:url('https://cdn5.telesco.pe/file/abc.jpg')\"></a>"
            '<div class="tgme_widget_message_text js-message_text" dir="auto">A notice claiming to be from '
            "<b>@ugc_india</b> is <i>#Fake</i></div><time datetime=\"2021-12-13T10:09:14+00:00\"></time></div>")
    [post] = parse_telegram_posts(page)
    assert post.url == "https://t.me/PIB_FactCheck/125" and post.published == "2021-12-13"
    assert post.images == ["https://cdn5.telesco.pe/file/abc.jpg"] and "@ugc_india" in post.text


def test_fact_check_card_yields_the_bare_notice(fake_image):
    card = Image.new("RGB", (900, 1300), (150, 190, 235))
    sheet = fake_image.convert("RGB").resize((700, round(fake_image.height * 700 / fake_image.width)))
    card.paste(sheet, (100, 150))
    ImageDraw.Draw(card).ellipse((400, 500, 700, 800), outline=(220, 30, 30), width=18)  # the FAKE stamp
    out = extract_document(png_bytes(card))
    assert out is not None
    got = np.asarray(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
    assert abs(got.shape[1] - 700) < 20
    red = (got[..., 0] > 140) & (got[..., 0] - got[..., 1] > 60) & (got[..., 0] - got[..., 2] > 60)
    assert red.sum() == 0


def test_pasted_text_excerpt(index):
    db, store, _ = index
    v = Verifier(db, store, use_ocr=False)
    excerpt = " ".join(OFFICIAL[7:11])
    assert v.verify_text(excerpt).verdict == VERIFIED_CONTENT
    changed = excerpt.replace("15 November", "25 November")
    res = v.verify_text(changed)
    assert res.verdict == ALTERED
    [change] = res.match.text.changes
    assert (change.official, change.submitted) == ("15", "25")
    assert v.verify_text("too short").verdict == UNREADABLE


def test_withdrawn_notice_is_still_verified_but_labelled(index, official_pdf):
    db, store, _ = index
    db.conn.execute("UPDATE notices SET removed_on='2026-11-01T00:00:00+00:00' WHERE record_id='1001'")
    db.commit()
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert res.verdict == VERIFIED_EXACT and res.withdrawn == "2026-11-01"
    from circverify.htmlreport import render_result
    assert "withdrawn from ugc.gov.in" in render_result(res)


def test_archive_only_document_is_verified_but_labelled(index, official_pdf):
    db, store, _ = index
    db.conn.execute("UPDATE notices SET source='archive' WHERE record_id='1001'")
    url = db.conn.execute("SELECT url FROM links").fetchone()[0]
    db.save_capture(url, "2019-03-27T15:36:42+00:00", None, 0)
    db.commit()
    res = Verifier(db, store, use_ocr=False).verify(official_pdf)
    assert res.verdict == VERIFIED_EXACT and res.archived_only == "2019-03-27" and res.withdrawn is None
    from circverify.htmlreport import render_result
    from circverify.telegram import format_reply

    page = render_result(res)
    assert "no longer listed on ugc.gov.in" in page and "First archived" in page
    assert "Wayback Machine" in format_reply(res)


def test_not_found_signals_are_hints_only(index):
    db, store, _ = index
    fake = make_pdf([FAKE_LINES])
    res = Verifier(db, store, use_ocr=False).verify(fake)
    assert res.verdict == NO_MATCH
    assert any("reference number" in n and "belongs to a different official document" in n for n in res.signal_notes)
    assert res.fields["ref_no"] == "1-15/2026 (CPP-II)"


def test_receipts_store_outcomes_not_documents(index):
    from circverify.receipts import create_receipt, load_receipt

    db, store, _ = index
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    res = Verifier(db, store, use_ocr=False).verify(make_pdf([lines]), "forged.pdf")
    rid = create_receipt(db, res)
    payload, created = load_receipt(db, rid)
    assert payload["verdict"] == ALTERED and payload["changes"][0]["submitted"] == "25"
    assert payload["official"]["url"].endswith("/1001.pdf")
    assert "image" not in json.dumps(payload).lower() or "data:image" not in json.dumps(payload)


def test_web_receipts_text_feed_coverage_and_fingerprint(index, tmp_path):
    from fastapi.testclient import TestClient

    from circverify.web import create_app

    db, store, sha = index
    client = TestClient(create_app(tmp_path))
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    frag = client.post("/verify?fragment=1", files={"file": ("f.pdf", make_pdf([lines]), "application/pdf")}).text
    token = re.search(r'data-token="([^"]+)"', frag).group(1)
    made = client.post("/receipt", data={"token": token}).json()
    page = client.get(made["url"])
    assert page.status_code == 200 and "Altered" in page.text and "25" in page.text
    assert client.post("/receipt", data={"token": "expired"}).status_code == 404

    txt = client.post("/verify-text?fragment=1", data={"text": " ".join(OFFICIAL[7:11])})
    assert "Verified" in txt.text and "word for word" in txt.text

    feed = client.get("/feed.xml")
    assert feed.headers["content-type"].startswith("application/atom+xml") and "<entry>" in feed.text
    assert "Coverage and blind spots" in client.get("/coverage").text
    assert "Verified" in client.get(f"/v/{sha}").text
    assert "Not found" in client.get("/v/" + "0" * 64).text


def test_telegram_bot_replies(index, tmp_path, official_render):
    from circverify.telegram import TelegramBot

    sent = []
    png = png_bytes(official_render)

    def api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/1.png", "file_size": len(png)}})
        if path.startswith("/file/"):
            return httpx.Response(200, content=png)
        if path.endswith("/sendMessage"):
            sent.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404)

    bot = TelegramBot("T0KEN", tmp_path, transport=httpx.MockTransport(api))
    bot.handle({"message": {"message_id": 1, "chat": {"id": 7}, "text": "/start"}})
    bot.handle({"message": {"message_id": 2, "chat": {"id": 7}, "photo": [{"file_id": "a"}, {"file_id": "b"}]}})
    bot.handle({"message": {"message_id": 3, "chat": {"id": 7}, "text": " ".join(OFFICIAL[7:11])}})
    assert "unofficial" in sent[0].lower()
    assert sent[1].startswith("VERIFIED") and "Official file:" in sent[1]
    assert sent[2].startswith("VERIFIED")
    assert all(s.rstrip().endswith("not affiliated with UGC.") for s in sent[1:])
    bot.handle({"message": {"message_id": 4, "chat": {"id": 7}, "text": " ".join(FAKE_LINES[4:])}})
    assert 'Reply "report"' in sent[-1]
    bot.handle({"message": {"message_id": 5, "chat": {"id": 7}, "text": "report college group"}})
    assert "review queue" in sent[-1]
    assert bot.db.reports("new")[0]["channel"] == "telegram"
