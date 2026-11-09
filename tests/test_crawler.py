from __future__ import annotations

import httpx

from pdfgen import make_pdf

from circverify.crawler import Fetcher, download_all, sync_listings
from circverify.db import Database
from circverify.indexer import index_pending
from circverify.issuers.base import Link, Notice
from circverify.store import BlobStore

PDF = make_pdf([["A real notice", "with some text on it for the index"]])


def handler(calls):
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/ok.pdf":
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if path == "/soft404.pdf":  # what ugc.gov.in does for missing files
            return httpx.Response(200, content=b"<!DOCTYPE html><html><body>Error</body></html>")
        if path == "/gone.pdf":
            return httpx.Response(404)
        if path == "/flaky.pdf":
            if calls[path] == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=PDF)
        if path == "/word.pdf":
            return httpx.Response(200, content=b"PK\x03\x04 a docx file")
        return httpx.Response(500)
    return respond


class FakeIssuer:
    slug = "test"

    def list_notices(self, client, sources=None):
        base = "https://example.test"
        yield Notice("test", "notices", "1", "Good", "2026-01-01",
                     [Link(f"{base}/ok.pdf"), Link(f"{base}/flaky.pdf", "Hindi")])
        yield Notice("test", "notices", "2", "Broken", None,
                     [Link(f"{base}/soft404.pdf"), Link(f"{base}/gone.pdf"), Link(f"{base}/word.pdf")])


def test_crawl_records_every_outcome(tmp_path):
    calls: dict = {}
    db = Database(tmp_path / "i.sqlite3")
    store = BlobStore(tmp_path)
    f = Fetcher(delay=0, backoff=0, transport=httpx.MockTransport(handler(calls)))

    res = sync_listings(db, FakeIssuer(), f)
    assert (res.seen, res.new, res.links) == (2, 2, 5)
    counts = download_all(db, store, f, db.urls_to_fetch(), workers=2)
    assert dict(counts) == {"ok": 2, "dead": 2, "not_document": 1}
    assert calls["/flaky.pdf"] == 2  # retried once after the 503

    # ok.pdf and flaky.pdf serve the same bytes: stored once.
    assert db.stats()["files"] == 1
    # Nothing left to fetch, and dead links are not retried unless asked.
    assert db.urls_to_fetch() == []
    assert len(db.urls_to_fetch(retry_failed=True)) == 3

    # Re-syncing does not duplicate anything.
    assert sync_listings(db, FakeIssuer(), f).new == 0

    index_pending(db, store, workers=1, use_ocr=False)
    sha = db.conn.execute("SELECT sha256 FROM files").fetchone()[0]
    prov = db.provenance(sha)
    assert {p.url.rsplit("/", 1)[1] for p in prov} == {"ok.pdf", "flaky.pdf"}
    assert db.search("notice")[0]["sha256"] == sha
    db.close()


def test_replaced_file_keeps_history(tmp_path):
    db = Database(tmp_path / "i.sqlite3")
    store = BlobStore(tmp_path)
    url = "https://example.test/n.pdf"
    db.upsert_notice(Notice("test", "notices", "1", "N", None, [Link(url)]))
    old, new = store.put_bytes(b"%PDF-1.4 old"), store.put_bytes(b"%PDF-1.4 new")
    db.record_fetch(url, status="ok", sha256=old, fetched_at="2026-01-01T00:00:00+00:00")
    db.record_fetch(url, status="ok", sha256=new, fetched_at="2026-02-01T00:00:00+00:00")
    assert db.provenance(old)[0].is_current is False
    assert db.provenance(new)[0].is_current is True
    db.close()


def test_ocr_fills_scanned_pages(tmp_path):
    from circverify.indexer import index_document
    from PIL import Image
    import io

    img = Image.new("L", (800, 1100), 255)
    for y in range(100, 1000, 40):
        img.paste(0, (100, y, 700, y + 12))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    res = index_document(buf.getvalue(), "x", "png", ocr=lambda im: "text recognised from the scan")
    assert res.pages[0]["text_source"] == "ocr" and res.ocr_pages == 1
    assert res.pages[0]["n_shingles"] > 0
