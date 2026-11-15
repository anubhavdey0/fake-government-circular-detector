from __future__ import annotations

import re

import pytest

from conftest import DEADLINE_LINE, OFFICIAL, UNRELATED
from pdfgen import make_pdf

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circverify.htmlreport import render_report  # noqa: E402
from circverify.matcher import Verifier  # noqa: E402
from circverify.web import create_app  # noqa: E402


def forged() -> bytes:
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    return make_pdf([lines])


@pytest.fixture()
def client(index, tmp_path):
    return TestClient(create_app(tmp_path))


def test_home_shows_the_index_and_latest_documents(client, index):
    home = client.get("/")
    assert home.status_code == 200
    assert "Check a circular before you forward it." in home.text
    assert ">2<" in home.text  # the document count
    assert "Latest from UGC" in home.text and "/thumb/" in home.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/fonts/Geist-Variable.woff2").status_code == 200
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg")


def test_checks_render_a_case_file(client):
    files = {"file": ("forged.pdf", forged(), "application/pdf")}
    frag = client.post("/verify?fragment=1", files=files)
    assert frag.status_code == 200 and "<html" not in frag.text
    assert 'id="verdict-title">Altered<' in frag.text
    assert 'class="hl-off">15<' in frag.text and 'class="hl-sub">25<' in frag.text
    assert 'class="viewer"' in frag.text and "Official source" in frag.text

    full = client.post("/verify", files={"file": ("forged.pdf", forged(), "application/pdf")})
    assert "<html" in full.text and 'class="has-result"' in full.text

    api = client.post("/api/verify", files={"file": ("forged.pdf", forged(), "application/pdf")}).json()
    assert api["verdict"] == "ALTERED" and api["match"]["text_changes"][0]["official"] == "15"

    empty = client.post("/verify?fragment=1", files={"file": ("x.pdf", b"", "application/pdf")})
    assert empty.status_code == 400


def test_not_found_offers_a_search(client):
    frag = client.post("/verify?fragment=1", files={"file": ("x.pdf", make_pdf([UNRELATED]), "application/pdf")})
    assert "Not found" in frag.text and 'action="/search"' in frag.text


def test_report_download(client):
    r = client.post("/report", files={"file": ("forged notice.pdf", forged(), "application/pdf")})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and "forged-notice" in r.headers["content-disposition"]
    assert "Verification report" in r.text and "@font-face" in r.text


def test_search(client):
    page = client.get("/search?q=fellowship scheme")
    assert page.status_code == 200
    assert "Extension of last date" in page.text and "<mark>" in page.text
    assert "Try a few words" in client.get("/search").text
    assert "Nothing in the index mentions" in client.get("/search?q=zzzqqq").text
    # Characters that are FTS5 syntax must not break the query.
    assert client.get('/search?q="fellowship" OR -(').status_code == 200


def test_thumbnails(client, index):
    _, _, sha = index
    img = client.get(f"/thumb/{sha}?w=200")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get("/thumb/not-a-hash").status_code == 404
    assert client.get("/thumb/" + "0" * 64).status_code == 404


def test_unknown_page_is_a_friendly_404(client):
    r = client.get("/nope", headers={"accept": "text/html"})
    assert r.status_code == 404 and "This page is not here." in r.text


def test_report_escapes_and_loads_nothing_from_elsewhere(index):
    db, store, _ = index
    res = Verifier(db, store, use_ocr=False).verify(forged(), filename="<script>alert(1)</script>.pdf")
    html = render_report(res)
    assert "<script>alert(1)</script>" not in html
    assert "Verification report" in html
    # Links to the official file are fine; nothing may be *loaded* from elsewhere.
    assert not re.search(r"""\bsrc=["']?(https?:)?//""", html)
    assert "<link" not in html and "@import" not in html and "url(http" not in html


def test_public_server_limits_checks_per_address_and_while_busy(index, tmp_path):
    from circverify.web import _Guard

    limited = TestClient(create_app(tmp_path, rate_limit=(2, 600)))
    form = {"text": "The last date for fellowship applications is extended."}
    assert limited.post("/verify-text", data=form).status_code == 200
    assert limited.post("/verify-text", data=form).status_code == 200
    third = limited.post("/verify-text", data=form)
    assert third.status_code == 429 and "limit of 2 checks" in third.text and third.headers["retry-after"]
    assert limited.get("/search?q=fellowship").status_code == 200  # browsing is never limited

    guard = _Guard(None, max_waiting=1)
    assert guard.admit("a") is None
    assert guard.admit("b") == ("busy", 60)
    guard.done()
    assert guard.admit("b") is None
