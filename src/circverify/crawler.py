"""Polite crawling: sync listings, then download every linked file once."""

from __future__ import annotations

import itertools
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import USER_AGENT
from .db import Database, utcnow
from .extract import sniff_kind
from .issuers.base import Issuer
from .store import BlobStore, FileTooLarge

MAX_FILE_BYTES = 150 * 1024 * 1024
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchOutcome:
    url: str
    status: str  # ok | dead | not_document | too_large | error
    http_status: int | None = None
    final_url: str | None = None
    sha256: str | None = None
    size: int | None = None
    kind: str | None = None
    error: str | None = None
    origin: str = "live"


class Fetcher:
    """An httpx client with a shared rate limit and retry on transient failures."""

    def __init__(
        self,
        *,
        delay: float = 0.5,
        timeout: float = 90.0,
        retries: int = 3,
        backoff: float = 2.0,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.delay = delay
        self.retries = retries
        self.backoff = backoff
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout, connect=30.0),
            follow_redirects=True,
            transport=transport,
        )
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def close(self) -> None:
        self.client.close()

    def throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self.delay
        if start > now:
            time.sleep(start - now)

    def download(self, url: str, store: BlobStore, *, max_bytes: int = MAX_FILE_BYTES,
                 fetch_url: str | None = None, origin: str = "live") -> FetchOutcome:
        """GET ``fetch_url`` (default ``url``) into the store, recording it against ``url``."""
        target = fetch_url or url
        last_error = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(60.0, self.backoff * 2 ** (attempt - 1)))
            self.throttle()
            try:
                with self.client.stream("GET", target) as resp:
                    final = str(resp.url)
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
                    return FetchOutcome(url, "ok", resp.status_code, final, sha, size, kind, origin=origin)
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


def _looks_like_html(head: bytes) -> bool:
    sample = head[:512].lstrip().lower()
    return sample.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")) or b"<html" in sample


@dataclass
class SyncResult:
    seen: int
    new: int
    links: int
    by_source: Counter


def sync_listings(db: Database, issuer: Issuer, fetcher: Fetcher, sources: list[str] | None = None) -> SyncResult:
    """Fetch the issuer's listings and upsert every notice and link."""
    now = utcnow()
    seen = new = links = 0
    by_source: Counter = Counter()
    for notice in issuer.list_notices(fetcher.client, sources):
        _, is_new = db.upsert_notice(notice, now)
        seen += 1
        new += is_new
        links += len(notice.links)
        by_source[notice.source] += 1
    db.set_meta("last_listing_sync", now)
    db.commit()
    return SyncResult(seen, new, links, by_source)


def download_all(
    db: Database,
    store: BlobStore,
    fetcher: Fetcher,
    urls: Iterable[str],
    *,
    workers: int = 2,
    wayback: bool = False,
    on_result: Callable[[FetchOutcome], None] | None = None,
) -> Counter:
    """Download ``urls`` concurrently (bounded by the fetcher's rate limit) and record each result."""
    counts: Counter = Counter()

    def job(url: str) -> FetchOutcome:
        if wayback:
            snap = fetcher.wayback_snapshot(url)
            if not snap:
                return FetchOutcome(url, "dead", error="no Wayback Machine capture", origin="wayback")
            return fetcher.download(url, store, fetch_url=snap, origin="wayback")
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
                )
            if out.status == "ok" and out.sha256:
                db.add_file(out.sha256, out.size or 0, out.kind or "unknown")
            counts[out.status] += 1
            # Commit every result: never hold the write lock across network waits.
            db.commit()
            if on_result:
                on_result(out)
    db.set_meta("last_download", utcnow())
    db.commit()
    return counts
