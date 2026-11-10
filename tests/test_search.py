"""Search by words or reference number, filters, the public registry and the search API."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circverify.web import create_app  # noqa: E402


@pytest.fixture()
def client(index, tmp_path):
    return TestClient(create_app(tmp_path))


def test_registry_lists_every_document(client):
    page = client.get("/search").text
    assert "All documents" in page and "1 to 2 of 2" in page
    assert "Extension of last date for fellowship applications" in page and "Inter-university athletics" in page
    assert "/v/" in page  # each entry links to its hash check


def test_reference_number_search(client):
    page = client.get("/search", params={"q": "F.No. 1-15/2026 (CPP-II)"}).text
    assert "carry the reference number" in page and "fellowship" in page
    assert "athletics" not in page


def test_word_search_with_filters(client, index):
    db, _, _ = index
    assert "fellowship" in client.get("/search", params={"q": "fellowship"}).text
    none = client.get("/search", params={"q": "fellowship", "status": "withdrawn"}).text
    assert "with these filters" in none
    late = client.get("/search", params={"q": "fellowship", "from": "2026-12-01"}).text
    assert "with these filters" in late
    db.conn.execute("UPDATE notices SET removed_on='2026-11-05T00:00:00+00:00' WHERE record_id='1001'")
    db.commit()
    withdrawn = client.get("/search", params={"q": "fellowship", "status": "withdrawn"}).text
    assert "withdrawn 5 Nov 2026" in withdrawn


def test_search_api(client):
    body = client.get("/api/search", params={"q": "athletics"}).json()
    assert body["results"][0]["title"] == "Inter-university athletics meet"
    reg = client.get("/api/search").json()
    assert reg["total"] == 2 and {r["source"] for r in reg["results"]} == {"notices"}
    assert client.get("/api/search", params={"q": "1-15/2026"}).json()["results"][0]["source"] == "notices"


def test_several_files_at_once(client, official_pdf):
    from conftest import UNRELATED
    from pdfgen import make_pdf

    files = [("files", ("a.pdf", official_pdf, "application/pdf")),
             ("files", ("b.pdf", make_pdf([UNRELATED]), "application/pdf")),
             ("files", ("c.txt", b"", "text/plain"))]
    page = client.post("/verify-bulk", files=files).text
    assert "3 files checked" in page and "Verified" in page and "Not found" in page
    assert "Not checked" in page and "https://www.ugc.gov.in/pdfnews/1001.pdf" in page
