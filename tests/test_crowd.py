"""Crowd reports (a review queue, never a verdict) and the WhatsApp bot."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json

import httpx
import pytest

from conftest import OFFICIAL, png_bytes
from pdfgen import make_pdf

from circverify.crowd import create_report, dismiss, export, promote
from circverify.matcher import KNOWN_FAKE, NO_MATCH, Verifier

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circverify.web import create_app  # noqa: E402
from circverify.whatsapp import WhatsAppConfig  # noqa: E402

FAKE = make_pdf([[*OFFICIAL[:3], "Subject: Offline examinations in all universities",
                  "All universities are hereby advised to conduct the coming semester",
                  "examinations in offline mode at the home centres of the students.",
                  "Students must register at https://ugc-gov-in.com/register by 30 November 2026.", "(Secretary)"]])


def test_reports_queue_counts_and_never_changes_a_verdict(index, official_pdf):
    db, store, _ = index
    v = Verifier(db, store, use_ocr=False)
    rid, status = create_report(db, store, v, data=official_pdf, filename="real.pdf")
    assert rid is None and status.startswith("refused: this is the official")

    rid, status = create_report(db, store, v, data=FAKE, filename="fake.pdf", seen_where="college group")
    assert status == "added"
    again, status = create_report(db, store, v, data=FAKE, filename="fake.pdf", seen_where="family group",
                                  debunk_url="javascript:alert(1)")
    assert (again, status) == (rid, "counted")
    row = db.report(rid)
    assert row["count"] == 2 and "college group" in row["seen_where"] and "family group" in row["seen_where"]
    assert row["debunk_url"] is None  # only http(s) links are kept
    # A report on its own does not make anything fake.
    assert Verifier(db, store, use_ocr=False).verify(FAKE).verdict == NO_MATCH


def test_promoting_a_report_needs_a_public_debunk(index, tmp_path):
    db, store, _ = index
    v = Verifier(db, store, use_ocr=False)
    rid, _ = create_report(db, store, v, data=FAKE, filename="fake.pdf")
    assert promote(db, store, rid, url="not a url", source="UGC")[0] is None
    sha, status = promote(db, store, rid, url="https://www.ugc.gov.in/pdfnews/caution.pdf", source="UGC",
                          verifier=v)
    assert sha and status == "added" and db.report(rid)["status"] == "promoted"
    assert Verifier(db, store, use_ocr=False).verify(FAKE).verdict == KNOWN_FAKE

    tid, _ = create_report(db, store, v, text="All universities are hereby advised to conduct the coming semester "
                                              "examinations in offline mode at the home centres of the students.")
    assert promote(db, store, tid, url="https://example.org/debunk", source="PIB")[0] is None
    assert dismiss(db, tid, "duplicate") and db.report(tid)["status"] == "dismissed"

    n = export(db, store, tmp_path / "out")
    rows = list(csv.DictReader((tmp_path / "out" / "reports.csv").open(encoding="utf-8")))
    assert n == 2 and {r["status"] for r in rows} == {"promoted", "dismissed"}
    assert all((tmp_path / "out" / r["file"]).exists() for r in rows)


def test_report_button_and_route(index, tmp_path, official_pdf):
    client = TestClient(create_app(tmp_path))
    nf = client.post("/verify?fragment=1", files={"file": ("f.pdf", FAKE, "application/pdf")}).text
    assert 'data-action="report-fake"' in nf and "never changes a verdict" in nf
    ok = client.post("/verify?fragment=1", files={"file": ("o.pdf", official_pdf, "application/pdf")}).text
    assert 'data-action="report-fake"' not in ok

    refused = client.post("/report-fake", files={"file": ("o.pdf", official_pdf, "application/pdf")}).json()
    assert refused["ok"] is False and "official" in refused["message"]
    sent = client.post("/report-fake", files={"file": ("f.pdf", FAKE, "application/pdf")},
                       data={"seen_where": "WhatsApp group", "debunk_url": "https://factcheck.example/1"}).json()
    assert sent["ok"] is True and "review queue" in sent["message"]


# -- WhatsApp -----------------------------------------------------------------------

CFG = WhatsAppConfig(token="T", phone_id="111", verify_token="hello", app_secret="s3cret")


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    return body, {"x-hub-signature-256": f"sha256={sig}", "content-type": "application/json"}


def _message(mid: str, **msg) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "messages": [{"from": "919800000000", "id": mid, **msg}]}}]}]}


@pytest.fixture()
def wa(index, tmp_path, official_render):
    sent: list[dict] = []
    png = png_bytes(official_render)

    def graph(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer T"
        if request.url.path.endswith("/111/messages"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
        if request.url.path.endswith("/MEDIA1"):
            return httpx.Response(200, json={"url": "https://lookaside.example/media1", "file_size": len(png)})
        if request.url.host == "lookaside.example":
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    client = TestClient(create_app(tmp_path, whatsapp=CFG, whatsapp_transport=httpx.MockTransport(graph)))
    return client, sent, index


def test_whatsapp_webhook_handshake_and_signatures(wa):
    client, sent, _ = wa
    ok = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "hello",
                                                 "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                   "hub.challenge": "1"}).status_code == 403
    body, headers = _signed(_message("m0", type="text", text={"body": "hi"}))
    assert client.post("/whatsapp/webhook", content=body,
                       headers={**headers, "x-hub-signature-256": "sha256=bad"}).status_code == 401
    assert client.post("/whatsapp/webhook", content=body, headers=headers).status_code == 200
    assert "Send me a UGC circular" in sent[-1]["text"]["body"]


def test_whatsapp_checks_photos_and_text_and_takes_reports(wa):
    client, sent, (db, _, _) = wa
    body, headers = _signed(_message("m1", type="image", image={"id": "MEDIA1", "mime_type": "image/png"}))
    client.post("/whatsapp/webhook", content=body, headers=headers)
    reply = sent[-1]
    assert reply["to"] == "919800000000" and reply["context"] == {"message_id": "m1"}
    assert reply["text"]["body"].startswith("VERIFIED")
    # The same delivery again (Meta retries) is not answered twice.
    client.post("/whatsapp/webhook", content=body, headers=headers)
    assert len(sent) == 1

    fake_text = ("All universities are hereby advised to conduct the coming semester examinations in offline "
                 "mode at the home centres of the students. Register at https://ugc-gov-in.com/register.")
    body, headers = _signed(_message("m2", type="text", text={"body": fake_text}))
    client.post("/whatsapp/webhook", content=body, headers=headers)
    assert sent[-1]["text"]["body"].startswith("NOT FOUND") and 'Reply "report"' in sent[-1]["text"]["body"]
    body, headers = _signed(_message("m3", type="text", text={"body": "report family group"}))
    client.post("/whatsapp/webhook", content=body, headers=headers)
    assert "review queue" in sent[-1]["text"]["body"]
    [row] = db.reports("new")
    assert row["channel"] == "whatsapp" and row["seen_where"] == "family group"
