"""Polite crawling: sync listings, download every linked file once, re-check, and backfill from the archive.

Rules this module keeps:

* robots.txt is honoured for every host it fetches from;
* one shared rate limit across all worker threads;
* nothing is ever overwritten: every download is a new row in ``fetches``,
  and files are stored by content hash, so earlier versions stay on record;
* a notice that disappears from the issuer's listing is marked with the
  date it was removed, not deleted.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timedelta, timezone
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import APP_NAME, USER_AGENT
from .db import Database, utcnow
from .extract import sniff_kind
from .issuers.base import Issuer, Link, Notice
from .store import BlobStore, FileTooLarge

MAX_FILE_BYTES = 150 * 1024 * 1024
_RETRY_STATUS = {429, 500, 502, 503, 504}
CDX = "http://web.archive.org/cdx/search/cdx"


@dataclass
class FetchOutcome:
    url: str
    status: str  # ok | dead | not_document | too_large | error | robots
    http_status: int | None = None
    final_url: str | None = None
    sha256: str | None = None
    size: int | None = None
    kind: str | None = None
    error: str | None = None
    origin: str = "live"
    etag: str | None = None
    last_modified: str | None = None
    changed: bool | None = None  # on a re-check: did the file differ from the stored version?


class Fetcher:
    """An httpx client with a shared rate limit, robots.txt checks and retry on transient failures."""

    def __init__(
        self,
        *,
        delay: float = 0.5,
        timeout: float = 90.0,
        retries: int = 3,
        backoff: float = 2.0,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        respect_robots: bool = True,
    ):
        self.delay = delay
        self.retries = retries
        self.backoff = backoff
        self.respect_robots = respect_robots
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout, connect=30.0),
            follow_redirects=True,
            transport=transport,
        )
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self.delay
        if start > now:
            time.sleep(start - now)

    # -- robots.txt -----------------------------------------------------------

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin not in self._robots:
                self._robots[origin] = self._load_robots(origin)
            rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(APP_NAME, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        self.throttle()
        try:
            resp = self.client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return None
        body = resp.text if resp.status_code == 200 else ""
        # Some sites (ugc.gov.in among them) answer /robots.txt with their HTML error page.
        if not body or _looks_like_html(body.encode("utf-8", "ignore")[:512]):
            return None
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return rp

    # -- downloads ------------------------------------------------------------

    def download(self, url: str, store: BlobStore, *, max_bytes: int = MAX_FILE_BYTES,
                 fetch_url: str | None = None, origin: str = "live", etag: str | None = None,
                 last_modified: str | None = None, known_sha: str | None = None) -> FetchOutcome:
        """GET ``fetch_url`` (default ``url``) into the store, recording it against ``url``.

        With ``etag``/``last_modified`` from an earlier fetch the request is
        conditional; an unchanged file costs one round trip and no download.
        """
        target = fetch_url or url
        if origin == "live" and not self.allowed(target):
            return FetchOutcome(url, "robots", error="disallowed by robots.txt", origin=origin)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        last_error = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(60.0, self.backoff * 2 ** (attempt - 1)))
            self.throttle()
            try:
                with self.client.stream("GET", target, headers=headers) as resp:
                    final = str(resp.url)
                    validators = {"etag": resp.headers.get("etag") or etag,
                                  "last_modified": resp.headers.get("last-modified") or last_modified}
                    if resp.status_code == 304 and known_sha:
                        return FetchOutcome(url, "ok", 304, final, known_sha, origin=origin, changed=False,
                                            **validators)
                    if resp.status_code in _RETRY_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    if resp.status_code >= 400 or "/ErrorPage" in final:
                        return FetchOutcome(url, "dead", resp.status_code, final, origin=origin)
                    length = resp.headers.get("content-length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        return FetchOutcome(url, "too_large", resp.status_code, final, size=int(length),
                                            origin=origin)
                    chunks = resp.iter_bytes(64 * 1024)
                    head = b""
                    for chunk in chunks:
                        head += chunk
                        if len(head) >= 2048:
                            break
                    kind = sniff_kind(head)
                    if kind is None:
                        # The site answers missing files with an HTML page and status 200.
                        status = "dead" if _looks_like_html(head) else "not_document"
                        return FetchOutcome(url, status, resp.status_code, final, origin=origin)
                    sha, size, _ = store.put_stream(itertools.chain([head], chunks), max_bytes)
                    changed = None if known_sha is None else sha != known_sha
                    return FetchOutcome(url, "ok", resp.status_code, final, sha, size, kind, origin=origin,
                                        changed=changed, **validators)
            except FileTooLarge as exc:
                return FetchOutcome(url, "too_large", size=exc.args[0], origin=origin)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        return FetchOutcome(url, "error", error=last_error, origin=origin)

    # -- Wayback Machine ------------------------------------------------------

    def wayback_snapshot(self, url: str) -> str | None:
        """URL of the raw archived bytes of ``url``, if the Wayback Machine has a 200 capture."""
        parts = urlsplit(url)
        variants = [url]
        if parts.hostname == "www.ugc.gov.in":
            variants.append(urlunsplit(("http", "www.ugc.ac.in", parts.path, parts.query, "")))
        for variant in variants:
            self.throttle()
            try:
                resp = self.client.get("https://archive.org/wayback/available", params={"url": variant})
                resp.raise_for_status()
                snap = (resp.json().get("archived_snapshots") or {}).get("closest") or {}
            except (httpx.HTTPError, ValueError):
                continue
            if snap.get("available") and str(snap.get("status")) == "200" and snap.get("timestamp"):
                return f"https://web.archive.org/web/{snap['timestamp']}id_/{variant}"
        return None

    def cdx_first_captures(self, prefix: str, page_size: int = 5000) -> Iterator[tuple[str, str]]:
        """``(original_url, first_capture_timestamp)`` for every archived 200 response under ``prefix``.

        Results are sorted by URL and then time, and ``collapse=urlkey`` keeps
        the first capture of each URL. Paging uses resume keys: the CDX server's
        page-count API does not work together with ``collapse``.
        Raises ``ArchiveUnavailable`` rather than returning a silently partial list.
        """
        # HTML captures are excluded: a missing UGC file answers with an HTML error page and
        # status 200, and that page being archived proves nothing about the document.
        params = [("url", prefix), ("matchType", "prefix"), ("filter", "statuscode:200"),
                  ("filter", "!mimetype:text/html"), ("collapse", "urlkey"), ("fl", "original,timestamp"),
                  ("output", "json"), ("limit", str(page_size)), ("showResumeKey", "true")]
        resume: str | None = None
        while True:
            rows = self._cdx_page(params + ([("resumeKey", resume)] if resume else []))
            if rows and rows[0] == ["original", "timestamp"]:
                rows = rows[1:]
            resume = None
            # With showResumeKey the last two rows are an empty row and [resume_key].
            if len(rows) >= 2 and rows[-2] == [] and len(rows[-1]) == 1:
                resume, rows = rows[-1][0], rows[:-2]
            for row in rows:
                if len(row) >= 2:
                    yield row[0], row[1]
            if not resume:
                return

    def _cdx_page(self, params: list[tuple[str, str]]) -> list:
        last_error = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(60.0, self.backoff * 2 ** (attempt - 1)))
            self.throttle()
            try:
                resp = self.client.get(CDX, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code in _RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise ArchiveUnavailable(f"HTTP {resp.status_code}")
            if not resp.text.strip():
                return []
            try:
                return resp.json()
            except ValueError as exc:
                raise ArchiveUnavailable("the Wayback Machine returned something other than JSON") from exc
        raise ArchiveUnavailable(last_error)


class ArchiveUnavailable(Exception):
    """The Wayback Machine's index could not be read."""


def _looks_like_html(head: bytes) -> bool:
    sample = head[:512].lstrip().lower()
    return sample.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")) or b"<html" in sample


def wayback_raw_url(ts: str, original: str) -> str:
    """The archived bytes exactly as captured (``id_``: no Wayback toolbar or rewriting)."""
    return f"https://web.archive.org/web/{ts}id_/{original}"


def wayback_time(ts: str) -> str:
    """``20131008064631`` -> ``2013-10-08T06:46:31+00:00``."""
    ts = (ts + "00000000000000")[:14]
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}+00:00"


@dataclass
class SyncResult:
    seen: int
    new: int
    links: int
    by_source: Counter
    removed: int = 0


def _sync_time(db: Database) -> str:
    """A timestamp later than the previous sync's, always with microseconds.

    "Not seen in this sync" is ``last_seen < now``, so two syncs must never share a time: the
    Windows clock can return the same value for calls milliseconds apart. The fixed format keeps
    the strings comparable as text."""
    now = datetime.now(timezone.utc)
    prev = db.get_meta("last_listing_sync")
    if prev:
        try:
            now = max(now, datetime.fromisoformat(prev) + timedelta(microseconds=1))
        except ValueError:
            pass
    return now.isoformat(timespec="microseconds")


def sync_listings(db: Database, issuer: Issuer, fetcher: Fetcher, sources: list[str] | None = None) -> SyncResult:
    """Fetch the issuer's listings, upsert every notice and link, and mark what is no longer listed."""
    now = _sync_time(db)
    wanted = list(sources or issuer.sources())
    for url in getattr(issuer, "source_urls", lambda s: [])(wanted):
        if not fetcher.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
    seen = new = links = 0
    by_source: Counter = Counter()
    for notice in issuer.list_notices(fetcher.client, wanted):
        _, is_new = db.upsert_notice(notice, now)
        seen += 1
        new += is_new
        links += len(notice.links)
        by_source[notice.source] += 1
    # Only sources that came back non-empty count as fully synced; an empty
    # response is more likely an outage than the issuer deleting everything.
    complete = [s for s in wanted if by_source.get(s)]
    removed = db.mark_removed(issuer.slug, complete, now) if complete else 0
    db.set_meta("last_listing_sync", now)
    db.commit()
    return SyncResult(seen, new, links, by_source, removed)


def download_all(
    db: Database,
    store: BlobStore,
    fetcher: Fetcher,
    urls: Iterable[str],
    *,
    workers: int = 2,
    wayback: bool = False,
    validators: dict[str, tuple[str, str | None, str | None]] | None = None,
    snapshots: dict[str, str] | None = None,
    on_result: Callable[[FetchOutcome], None] | None = None,
) -> Counter:
    """Download ``urls`` concurrently (bounded by the fetcher's rate limit) and record each result.

    ``validators`` maps a URL to ``(sha256, etag, last_modified)`` of its current
    version for a conditional re-check. ``snapshots`` maps a URL to a known
    Wayback Machine capture to fetch it from.
    """
    counts: Counter = Counter()

    def job(url: str) -> FetchOutcome:
        if snapshots and url in snapshots:
            return fetcher.download(url, store, fetch_url=snapshots[url], origin="wayback")
        if wayback:
            snap = fetcher.wayback_snapshot(url)
            if not snap:
                return FetchOutcome(url, "dead", error="no Wayback Machine capture", origin="wayback")
            return fetcher.download(url, store, fetch_url=snap, origin="wayback")
        if validators and url in validators:
            sha, etag, lm = validators[url]
            return fetcher.download(url, store, etag=etag, last_modified=lm, known_sha=sha)
        return fetcher.download(url, store)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(job, u) for u in urls]
        for fut in as_completed(futures):
            out = fut.result()
            # A failed Wayback lookup should not overwrite the live "dead" record with noise.
            if not (out.origin == "wayback" and out.status != "ok"):
                db.record_fetch(
                    out.url,
                    status=out.status,
                    http_status=out.http_status,
                    final_url=out.final_url,
                    sha256=out.sha256,
                    error=out.error,
                    origin=out.origin,
                    etag=out.etag,
                    last_modified=out.last_modified,
                )
            if out.status == "ok" and out.sha256 and out.http_status != 304:
                db.add_file(out.sha256, out.size or 0, out.kind or "unknown")
            key = out.status
            if out.changed is not None:
                key = "changed" if out.changed else "unchanged"
            counts[key] += 1
            # Commit every result: never hold the write lock across network waits.
            db.commit()
            if on_result:
                on_result(out)
    db.set_meta("last_download", utcnow())
    db.commit()
    return counts


def recheck_targets(db: Database) -> dict[str, tuple[str, str | None, str | None]]:
    """Every URL that currently serves a file, with what is needed for a conditional GET."""
    return {r["url"]: (r["sha256"], r["etag"], r["last_modified"]) for r in db.ok_fetches()}


# -- Wayback Machine backfill -----------------------------------------------------

_LEADING_ID = re.compile(r"^\d{4,}[_-]")


def title_from_filename(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"\.(pdf|jpe?g|png)$", "", name, flags=re.I)
    name = _LEADING_ID.sub("", name)
    name = re.sub(r"[_\-]+", " ", name).strip()
    return (name[:1].upper() + name[1:]) if name else "Untitled archived file"


def scan_archive(db: Database, issuer: Issuer, fetcher: Fetcher) -> tuple[dict[str, tuple[str, str]], int]:
    """Record the first Wayback Machine capture of every official URL.

    Returns ``(unlisted, recorded)``: archived URLs the issuer no longer lists,
    as ``{canonical_url: (original_url, timestamp)}`` (candidates for backfill),
    and how many listed URLs have a first-capture date. A complete scan
    replaces the previous one; nothing is written if the archive cannot be
    read completely.
    """
    # An archive-only URL whose download has not succeeded yet (an interrupted backfill) stays a
    # backfill candidate.
    listed = db.archived_urls()
    first: dict[str, tuple[str, str]] = {}  # canonical -> (timestamp, original)
    unlisted: dict[str, tuple[str, str]] = {}
    for prefix in getattr(issuer, "wayback_prefixes", ()):
        for original, ts in fetcher.cdx_first_captures(prefix):
            canonical = issuer.canonical(original) if hasattr(issuer, "canonical") else original
            if not canonical:
                continue
            if canonical in listed:
                if canonical not in first or first[canonical][0] > ts:
                    first[canonical] = (ts, original)
            elif canonical not in unlisted or unlisted[canonical][1] > ts:
                unlisted[canonical] = (original, ts)
    db.conn.execute("DELETE FROM archive_captures")
    for url, (ts, original) in first.items():
        db.save_capture(url, wayback_time(ts), None, 0, snapshot=wayback_raw_url(ts, original))
    db.commit()
    return unlisted, len(first)


def backfill(db: Database, store: BlobStore, fetcher: Fetcher, issuer: Issuer,
             unlisted: dict[str, tuple[str, str]], *, limit: int | None = None, workers: int = 1,
             on_result: Callable[[FetchOutcome], None] | None = None) -> Counter:
    """Download archived files the issuer no longer lists, as ``archive`` notices titled from the filename."""
    now = utcnow()
    # Newest first: recently removed notices are the ones still being forwarded.
    items = sorted(unlisted.items(), key=lambda kv: kv[1][1], reverse=True)
    if limit is not None:
        items = items[:limit]
    snapshots = {}
    for canonical, (original, ts) in items:
        db.upsert_notice(Notice(issuer=issuer.slug, source="archive", record_id=canonical,
                                title=title_from_filename(canonical), published=None,
                                links=[Link(canonical, "Wayback Machine")],
                                listing_url=f"https://web.archive.org/web/*/{original}"), now)
        db.save_capture(canonical, wayback_time(ts), None, 0)
        snapshots[canonical] = wayback_raw_url(ts, original)
    db.commit()
    return download_all(db, store, fetcher, list(snapshots), workers=workers, snapshots=snapshots,
                        on_result=on_result)
