"""SQLite index: notices, their links, fetch history, files and per-page fingerprints."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .issuers.base import Notice

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per entry on an issuer's listing page.
CREATE TABLE IF NOT EXISTS notices(
    id          INTEGER PRIMARY KEY,
    issuer      TEXT NOT NULL,
    source      TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    published   TEXT,
    listing_url TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE(issuer, source, record_id)
);

-- Each notice links to one or more files (English/Hindi versions, annexures).
CREATE TABLE IF NOT EXISTS links(
    id        INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    url       TEXT NOT NULL,
    label     TEXT,
    UNIQUE(notice_id, url)
);
CREATE INDEX IF NOT EXISTS links_url ON links(url);

-- Every download attempt. The newest row per URL is what the URL serves now;
-- older rows keep the history, so a silently replaced PDF is still on record.
CREATE TABLE IF NOT EXISTS fetches(
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'live',
    http_status INTEGER,
    final_url   TEXT,
    status      TEXT NOT NULL,
    sha256      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS fetches_url ON fetches(url, id);
CREATE INDEX IF NOT EXISTS fetches_sha ON fetches(sha256);

-- Unique file contents, keyed by SHA-256.
CREATE TABLE IF NOT EXISTS files(
    sha256        TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    n_pages       INTEGER,
    text_pages    INTEGER,
    ocr_pages     INTEGER,
    producer      TEXT,
    index_version INTEGER NOT NULL DEFAULT 0,
    index_error   TEXT
);

CREATE TABLE IF NOT EXISTS pages(
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL REFERENCES files(sha256),
    page_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    text_source TEXT NOT NULL,
    n_shingles  INTEGER NOT NULL,
    minhash     BLOB,
    phash       INTEGER,
    dhash       INTEGER,
    ink         REAL,
    UNIQUE(sha256, page_no)
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    text, content='pages', content_rowid='id', tokenize='unicode61'
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Provenance:
    """Where a file was found on the issuer's site."""

    issuer: str
    source: str
    title: str
    published: str | None
    url: str
    label: str | None
    listing_url: str | None
    first_fetched: str
    last_fetched: str
    origin: str
    is_current: bool


class Database:
    def __init__(self, path: Path | str, *, check_same_thread: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        # Only write when needed: a writer elsewhere (a running crawl) must not
        # stop the web server or a verify from opening the index to read.
        if self.get_meta("schema_version") != str(SCHEMA_VERSION):
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta -------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- listings ---------------------------------------------------------

    def upsert_notice(self, notice: Notice, now: str | None = None) -> tuple[int, bool]:
        """Insert or refresh a notice and its links. Returns ``(id, is_new)``."""
        now = now or utcnow()
        row = self.conn.execute(
            "SELECT id FROM notices WHERE issuer=? AND source=? AND record_id=?",
            (notice.issuer, notice.source, notice.record_id),
        ).fetchone()
        if row:
            notice_id = row["id"]
            self.conn.execute(
                "UPDATE notices SET title=?, published=?, listing_url=?, last_seen=? WHERE id=?",
                (notice.title, notice.published, notice.listing_url, now, notice_id),
            )
            is_new = False
        else:
            cur = self.conn.execute(
                "INSERT INTO notices(issuer, source, record_id, title, published, listing_url,"
                " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notice.issuer,
                    notice.source,
                    notice.record_id,
                    notice.title,
                    notice.published,
                    notice.listing_url,
                    now,
                    now,
                ),
            )
            notice_id = cur.lastrowid
            is_new = True
        for link in notice.links:
            self.conn.execute(
                "INSERT INTO links(notice_id, url, label) VALUES (?, ?, ?) "
                "ON CONFLICT(notice_id, url) DO UPDATE SET label=excluded.label",
                (notice_id, link.url, link.label),
            )
        return notice_id, is_new

    def urls_to_fetch(self, *, refresh: bool = False, retry_failed: bool = False) -> list[str]:
        """Linked URLs that still need downloading.

        By default: URLs never fetched, plus URLs whose last attempt was a
        transient error. ``retry_failed`` also retries dead links;
        ``refresh`` re-downloads everything.
        """
        if refresh:
            sql = "SELECT DISTINCT url FROM links ORDER BY url"
        else:
            retry = ("'error'", "'dead'", "'not_document'") if retry_failed else ("'error'",)
            sql = f"""
                SELECT DISTINCT l.url FROM links l
                LEFT JOIN (
                    SELECT url, status FROM fetches f
                    WHERE id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)
                ) last ON last.url = l.url
                WHERE last.url IS NULL OR last.status IN ({", ".join(retry)})
                ORDER BY l.url
            """
        return [r["url"] for r in self.conn.execute(sql)]

    def dead_urls(self) -> list[str]:
        sql = """
            SELECT f.url FROM fetches f
            WHERE f.id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)
              AND f.status IN ('dead', 'not_document')
            ORDER BY f.url
        """
        return [r["url"] for r in self.conn.execute(sql)]

    def record_fetch(
        self,
        url: str,
        *,
        status: str,
        http_status: int | None = None,
        final_url: str | None = None,
        sha256: str | None = None,
        error: str | None = None,
        origin: str = "live",
        fetched_at: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fetches(url, fetched_at, origin, http_status, final_url, status, sha256, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (url, fetched_at or utcnow(), origin, http_status, final_url, status, sha256, error),
        )

    def add_file(self, sha256: str, size: int, kind: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO files(sha256, size, kind, first_seen) VALUES (?, ?, ?, ?)",
            (sha256, size, kind, utcnow()),
        )
        return cur.rowcount > 0

    # -- indexing ---------------------------------------------------------

    def files_to_index(self, version: int, *, reindex: bool = False) -> list[sqlite3.Row]:
        if reindex:
            return list(self.conn.execute("SELECT sha256, kind, size FROM files ORDER BY size"))
        return list(
            self.conn.execute(
                "SELECT sha256, kind, size FROM files WHERE index_version < ? ORDER BY size",
                (version,),
            )
        )

    def replace_pages(
        self,
        sha256: str,
        pages: Iterable[dict],
        *,
        n_pages: int | None,
        text_pages: int,
        ocr_pages: int,
        producer: str | None,
        version: int,
        error: str | None = None,
    ) -> None:
        c = self.conn
        old = list(c.execute("SELECT id, text FROM pages WHERE sha256=?", (sha256,)))
        for row in old:
            c.execute(
                "INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', ?, ?)",
                (row["id"], row["text"]),
            )
        c.execute("DELETE FROM pages WHERE sha256=?", (sha256,))
        for p in pages:
            cur = c.execute(
                "INSERT INTO pages(sha256, page_no, text, text_source, n_shingles, minhash,"
                " phash, dhash, ink) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha256,
                    p["page_no"],
                    p["text"],
                    p["text_source"],
                    p["n_shingles"],
                    p["minhash"],
                    p["phash"],
                    p["dhash"],
                    p["ink"],
                ),
            )
            if p["text"]:
                c.execute("INSERT INTO pages_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, p["text"]))
        c.execute(
            "UPDATE files SET n_pages=?, text_pages=?, ocr_pages=?, producer=?, index_version=?,"
            " index_error=? WHERE sha256=?",
            (n_pages, text_pages, ocr_pages, producer, version, error, sha256),
        )

    # -- lookups ----------------------------------------------------------

    def file(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE sha256=?", (sha256,)).fetchone()

    def page_texts(self, sha256: str) -> list[tuple[int, str]]:
        return [
            (r["page_no"], r["text"])
            for r in self.conn.execute(
                "SELECT page_no, text FROM pages WHERE sha256=? ORDER BY page_no", (sha256,)
            )
        ]

    def provenance(self, sha256: str) -> list[Provenance]:
        """Every listing entry under which this exact file was served."""
        sql = """
            SELECT n.issuer, n.source, n.title, n.published, n.listing_url, l.url, l.label,
                   MIN(f.fetched_at) AS first_fetched, MAX(f.fetched_at) AS last_fetched,
                   MIN(f.origin) AS origin,
                   (SELECT f3.sha256 FROM fetches f3 WHERE f3.url = l.url
                     ORDER BY f3.id DESC LIMIT 1) AS current_sha
            FROM fetches f
            JOIN links l ON l.url = f.url
            JOIN notices n ON n.id = l.notice_id
            WHERE f.sha256 = ?
            GROUP BY n.id, l.url
            ORDER BY n.published DESC
        """
        out = []
        for r in self.conn.execute(sql, (sha256,)):
            out.append(
                Provenance(
                    issuer=r["issuer"],
                    source=r["source"],
                    title=r["title"],
                    published=r["published"],
                    url=r["url"],
                    label=r["label"],
                    listing_url=r["listing_url"],
                    first_fetched=r["first_fetched"],
                    last_fetched=r["last_fetched"],
                    origin=r["origin"],
                    is_current=r["current_sha"] == sha256,
                )
            )
        return out

    def search(self, query: str, limit: int = 10, *, marks: tuple[str, str] = ("[", "]")) -> list[sqlite3.Row]:
        sql = f"""
            SELECT p.sha256, p.page_no, snippet(pages_fts, 0, '{marks[0]}', '{marks[1]}', ' … ', 14) AS snippet,
                   bm25(pages_fts) AS score
            FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid
            WHERE pages_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """
        return list(self.conn.execute(sql, (query, limit)))

    def recent_documents(self, limit: int = 5) -> list[dict]:
        """The newest dated notices whose first file is downloaded and indexed."""
        sql = """
            SELECT n.id AS notice_id, n.title, n.published, n.source, l.url, f.sha256
            FROM notices n
            JOIN links l ON l.notice_id = n.id
            JOIN fetches f ON f.id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = l.url)
            JOIN files fi ON fi.sha256 = f.sha256
            WHERE f.status = 'ok' AND fi.kind = 'pdf' AND fi.index_version > 0 AND fi.index_error IS NULL
              AND n.published IS NOT NULL
            ORDER BY n.published DESC, n.id DESC, l.id ASC
            LIMIT ?
        """
        out, seen_notice, seen_sha = [], set(), set()
        for r in self.conn.execute(sql, (limit * 6,)):
            if r["notice_id"] in seen_notice or r["sha256"] in seen_sha:
                continue
            seen_notice.add(r["notice_id"])
            seen_sha.add(r["sha256"])
            out.append(dict(r))
            if len(out) == limit:
                break
        return out

    def iter_manifest(self) -> Iterator[sqlite3.Row]:
        """One row per (notice, link) with the content currently served."""
        sql = """
            SELECT n.issuer, n.source, n.record_id, n.published, n.title, l.label, l.url,
                   f.status, f.origin, f.fetched_at, f.sha256, fi.size, fi.n_pages,
                   fi.text_pages
            FROM links l
            JOIN notices n ON n.id = l.notice_id
            LEFT JOIN fetches f ON f.id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = l.url)
            LEFT JOIN files fi ON fi.sha256 = f.sha256
            ORDER BY n.published DESC, n.source, n.record_id, l.url
        """
        return self.conn.execute(sql)

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        latest = """
            SELECT status, COUNT(*) FROM fetches f
            WHERE id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)
            GROUP BY status
        """
        by_status = {r[0]: r[1] for r in self.conn.execute(latest)}
        by_source = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT issuer || '/' || source, COUNT(*) FROM notices GROUP BY 1 ORDER BY 1"
            )
        }
        return {
            "notices": q("SELECT COUNT(*) FROM notices"),
            "notices_by_source": by_source,
            "links": q("SELECT COUNT(DISTINCT url) FROM links"),
            "fetch_status": by_status,
            "files": q("SELECT COUNT(*) FROM files"),
            "files_bytes": q("SELECT COALESCE(SUM(size), 0) FROM files"),
            "files_indexed": q("SELECT COUNT(*) FROM files WHERE index_version > 0 AND index_error IS NULL"),
            "files_index_errors": q("SELECT COUNT(*) FROM files WHERE index_error IS NOT NULL"),
            "pages": q("SELECT COUNT(*) FROM pages"),
            "pages_with_text": q("SELECT COUNT(*) FROM pages WHERE text_source != 'none'"),
            "pages_ocr": q("SELECT COUNT(*) FROM pages WHERE text_source = 'ocr'"),
            "last_listing_sync": self.get_meta("last_listing_sync"),
            "last_download": self.get_meta("last_download"),
        }

    def commit(self) -> None:
        self.conn.commit()
