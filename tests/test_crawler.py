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

    def sources(self):
        return ["notices"]

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


class ShrinkingIssuer:
    """Lists two notices on the first sync and one on the second."""

    slug = "test"

    def __init__(self):
        self.calls = 0

    def sources(self):
        return ["notices"]

    def list_notices(self, client, sources=None):
        self.calls += 1
        yield Notice("test", "notices", "1", "Stays", None, [Link("https://example.test/a.pdf")])
        if self.calls == 1:
            yield Notice("test", "notices", "2", "Withdrawn later", None, [Link("https://example.test/b.pdf")])


def test_withdrawn_notices_are_marked_not_deleted(tmp_path):
    db = Database(tmp_path / "i.sqlite3")
    issuer = ShrinkingIssuer()
    f = Fetcher(delay=0, transport=httpx.MockTransport(lambda r: httpx.Response(404)), respect_robots=False)
    assert sync_listings(db, issuer, f).removed == 0
    res = sync_listings(db, issuer, f)
    assert res.removed == 1
    rows = dict(db.conn.execute("SELECT title, removed_on FROM notices").fetchall())
    assert rows["Stays"] is None and rows["Withdrawn later"] is not None
    db.close()


def test_two_syncs_in_one_clock_tick_still_mark_withdrawn_notices(tmp_path, monkeypatch):
    import circverify.crawler as crawler

    frozen = crawler.datetime(2026, 11, 11, 7, 0, 0, tzinfo=crawler.timezone.utc)

    class Clock(crawler.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(crawler, "datetime", Clock)
    db = Database(tmp_path / "i.sqlite3")
    issuer = ShrinkingIssuer()
    f = Fetcher(delay=0, transport=httpx.MockTransport(lambda r: httpx.Response(404)), respect_robots=False)
    sync_listings(db, issuer, f)
    assert sync_listings(db, issuer, f).removed == 1
    db.close()


def test_robots_txt_is_respected(tmp_path):
    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, content=PDF)

    store = BlobStore(tmp_path)
    f = Fetcher(delay=0, transport=httpx.MockTransport(respond))
    assert f.download("https://example.test/private/x.pdf", store).status == "robots"
    assert f.download("https://example.test/public/x.pdf", store).status == "ok"


def test_html_robots_page_means_no_rules(tmp_path):
    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="<!DOCTYPE html><html>Error</html>")
        return httpx.Response(200, content=PDF)

    f = Fetcher(delay=0, transport=httpx.MockTransport(respond))
    assert f.download("https://example.test/private/x.pdf", BlobStore(tmp_path)).status == "ok"


def test_recheck_uses_conditional_requests_and_keeps_old_versions(tmp_path):
    from circverify.crawler import recheck_targets

    state = {"version": 1}
    v2 = make_pdf([["A real notice", "silently replaced with different text"]])

    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if state["version"] == 1:
            if request.headers.get("if-modified-since") == "Mon, 01 Jan 2026 00:00:00 GMT":
                return httpx.Response(304)
            return httpx.Response(200, content=PDF, headers={"last-modified": "Mon, 01 Jan 2026 00:00:00 GMT"})
        return httpx.Response(200, content=v2, headers={"last-modified": "Tue, 02 Jun 2026 00:00:00 GMT"})

    db = Database(tmp_path / "i.sqlite3")
    store = BlobStore(tmp_path)
    url = "https://example.test/n.pdf"
    db.upsert_notice(Notice("test", "notices", "1", "N", None, [Link(url)]))
    f = Fetcher(delay=0, transport=httpx.MockTransport(respond))
    assert dict(download_all(db, store, f, [url])) == {"ok": 1}
    assert dict(download_all(db, store, f, [url], validators=recheck_targets(db))) == {"unchanged": 1}
    state["version"] = 2
    assert dict(download_all(db, store, f, [url], validators=recheck_targets(db))) == {"changed": 1}
    shas = [r[0] for r in db.conn.execute("SELECT DISTINCT sha256 FROM fetches WHERE sha256 IS NOT NULL")]
    assert len(shas) == 2 and all(store.has(s) for s in shas)
    old, new = sorted(shas, key=lambda s: db.provenance(s)[0].is_current)
    assert not db.provenance(old)[0].is_current and db.provenance(new)[0].is_current
    db.close()


def test_archived_titles_come_from_filenames():
    from circverify.crawler import title_from_filename, wayback_time

    assert title_from_filename("https://www.ugc.gov.in/pdfnews/0000579_tender-of-photocopy-job.pdf") == \
        "Tender of photocopy job"
    assert wayback_time("20131008064631") == "2013-10-08T06:46:31+00:00"


class ArchivedIssuer:
    slug = "test"
    wayback_prefixes = ("example.test/pdfnews/",)

    def sources(self):
        return ["notices"]

    def canonical(self, url):
        return url.replace("http://", "https://")

    def list_notices(self, client, sources=None):
        yield Notice("test", "notices", "1", "Listed", None, [Link("https://example.test/pdfnews/a.pdf")])


def test_archive_scan_pages_with_resume_keys_and_keeps_first_capture(tmp_path):
    from circverify.crawler import scan_archive

    pages = {
        None: [["original", "timestamp"], ["http://example.test/pdfnews/a.pdf", "20190101000000"],
               ["https://example.test/pdfnews/a.pdf", "20210101000000"], [], ["KEY1"]],
        "KEY1": [["original", "timestamp"], ["https://example.test/pdfnews/a.pdf", "20180505000000"],
                 ["https://example.test/pdfnews/old.pdf", "20120202000000"]],
    }
    seen = []

    def respond(request):
        key = request.url.params.get("resumeKey")
        seen.append(key)
        assert "showNumPages" not in request.url.params  # unsupported with collapse
        assert "!mimetype:text/html" in request.url.params.get_list("filter")  # soft-404 pages prove nothing
        return httpx.Response(200, json=pages[key])

    db = Database(tmp_path / "i.sqlite3")
    f = Fetcher(delay=0, transport=httpx.MockTransport(respond), respect_robots=False)
    sync_listings(db, ArchivedIssuer(), f)
    unlisted, recorded = scan_archive(db, ArchivedIssuer(), f)
    assert seen == [None, "KEY1"]
    assert recorded == 1
    assert list(unlisted) == ["https://example.test/pdfnews/old.pdf"]
    first = db.conn.execute("SELECT first_capture FROM archive_captures").fetchone()[0]
    assert first.startswith("2018-05-05")
    assert db.capture_snapshots(["https://example.test/pdfnews/a.pdf"]) == {
        "https://example.test/pdfnews/a.pdf": "https://web.archive.org/web/20180505000000id_/https://example.test/pdfnews/a.pdf"}
    db.close()


def test_archive_outage_is_reported_not_swallowed(tmp_path):
    import pytest

    from circverify.crawler import ArchiveUnavailable, scan_archive

    db = Database(tmp_path / "i.sqlite3")
    f = Fetcher(delay=0, backoff=0, retries=1, respect_robots=False,
                transport=httpx.MockTransport(lambda r: httpx.Response(503, text="<html>Offline</html>")))
    with pytest.raises(ArchiveUnavailable):
        scan_archive(db, ArchivedIssuer(), f)
    assert db.conn.execute("SELECT COUNT(*) FROM archive_captures").fetchone()[0] == 0
    db.close()


def test_interrupted_backfill_resumes_and_never_fetches_archive_links_live(tmp_path):
    from circverify.crawler import backfill, scan_archive

    archived = [["original", "timestamp"], ["https://example.test/pdfnews/old.pdf", "20120202000000"]]

    def respond(request):
        if request.url.host == "web.archive.org" and request.url.path.startswith("/cdx"):
            return httpx.Response(200, json=archived)
        if request.url.host == "web.archive.org":
            return httpx.Response(503)  # the archive fails mid-backfill
        return httpx.Response(200, text="<!DOCTYPE html><html>Error</html>")

    db = Database(tmp_path / "i.sqlite3")
    store = BlobStore(tmp_path)
    f = Fetcher(delay=0, backoff=0, retries=0, transport=httpx.MockTransport(respond), respect_robots=False)
    sync_listings(db, ArchivedIssuer(), f)
    unlisted, _ = scan_archive(db, ArchivedIssuer(), f)
    assert dict(backfill(db, store, f, ArchivedIssuer(), unlisted)).get("ok", 0) == 0
    old = "https://example.test/pdfnews/old.pdf"
    assert old not in db.urls_to_fetch() and old not in db.urls_to_fetch(refresh=True)
    unlisted, _ = scan_archive(db, ArchivedIssuer(), f)
    assert old in unlisted  # still a backfill candidate
    assert db.coverage()["archived_only_documents"] == 0
    db.close()
