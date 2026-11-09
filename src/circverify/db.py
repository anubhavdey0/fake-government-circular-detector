"""SQLite index: notices, their links, fetch history, files and per-page fingerprints."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .issuers.base import Notice

SCHEMA_VERSION = 2

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

-- Structured fields read from each file, plus a fingerprint of its letterhead.
CREATE TABLE IF NOT EXISTS fields(
    sha256       TEXT PRIMARY KEY,
    ref_no       TEXT,
    ref_key      TEXT,
    doc_date     TEXT,
    subject      TEXT,
    signatory    TEXT,
    designation  TEXT,
    header_phash INTEGER,
    header_ink   REAL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fields_ref ON fields(ref_key);

-- Public debunks by the issuer or a fact-checker, and the fake files they refer to.
CREATE TABLE IF NOT EXISTS debunks(
    id        INTEGER PRIMARY KEY,
    issuer    TEXT,
    source    TEXT NOT NULL,
    url       TEXT NOT NULL UNIQUE,
    published TEXT,
    claim     TEXT,
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fakes(
    sha256    TEXT NOT NULL,
    debunk_id INTEGER NOT NULL REFERENCES debunks(id),
    kind      TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    note      TEXT,
    PRIMARY KEY (sha256, debunk_id)
);
CREATE TABLE IF NOT EXISTS fake_pages(
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL,
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

-- Results people chose to share. No document is stored, only the outcome.
CREATE TABLE IF NOT EXISTS receipts(
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    filename   TEXT,
    verdict    TEXT NOT NULL,
    summary    TEXT NOT NULL,
    payload    TEXT NOT NULL
);

-- When the Wayback Machine first and last saw each official URL.
CREATE TABLE IF NOT EXISTS archive_captures(
    url           TEXT PRIMARY KEY,
    first_capture TEXT,
    last_capture  TEXT,
    captures      INTEGER,
    checked_at    TEXT NOT NULL
);
"""

# Columns added after version 1, applied in place to existing indexes.
_ADDED_COLUMNS = {
    "notices": [("removed_on", "TEXT")],
    "fetches": [("etag", "TEXT"), ("last_modified", "TEXT")],
}


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
    removed_on: str | None = None
    first_archived: str | None = None


class Database:
    def __init__(self, path: Path | str, *, check_same_thread: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Only write when needed: a writer elsewhere (a running crawl) must not
        # stop the web server or a verify from opening the index to read.
        if self.get_meta_safe("schema_version") != str(SCHEMA_VERSION):
            self.conn.executescript(SCHEMA)
            for table, cols in _ADDED_COLUMNS.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.commit()

    def get_meta_safe(self, key: str) -> str | None:
        try:
            return self.get_meta(key)
        except sqlite3.OperationalError:  # brand-new file: no meta table yet
            return None

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
                "UPDATE notices SET title=?, published=?, listing_url=?, last_seen=?, removed_on=NULL WHERE id=?",
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
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fetches(url, fetched_at, origin, http_status, final_url, status, sha256, error,"
            " etag, last_modified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, fetched_at or utcnow(), origin, http_status, final_url, status, sha256, error, etag,
             last_modified),
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

    def update_page_text(self, sha256: str, page: dict) -> None:
        """Replace one page's text and text fingerprint (after OCR), keeping the full-text index in step."""
        c = self.conn
        row = c.execute("SELECT id, text FROM pages WHERE sha256=? AND page_no=?", (sha256, page["page_no"])).fetchone()
        if row is None:
            return
        if row["text"]:
            c.execute("INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', ?, ?)", (row["id"], row["text"]))
        c.execute("UPDATE pages SET text=?, text_source=?, n_shingles=?, minhash=? WHERE id=?",
                  (page["text"], page["text_source"], page["n_shingles"], page["minhash"], row["id"]))
        if page["text"]:
            c.execute("INSERT INTO pages_fts(rowid, text) VALUES (?, ?)", (row["id"], page["text"]))
        c.execute(
            "UPDATE files SET text_pages=(SELECT COUNT(*) FROM pages WHERE sha256=? AND text_source IN ('layer', 'ocr')),"
            " ocr_pages=(SELECT COUNT(*) FROM pages WHERE sha256=? AND text_source = 'ocr') WHERE sha256=?",
            (sha256, sha256, sha256))

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
            SELECT n.issuer, n.source, n.title, n.published, n.listing_url, l.url, l.label, n.removed_on,
                   (SELECT first_capture FROM archive_captures a WHERE a.url = l.url) AS first_archived,
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
                    removed_on=r["removed_on"],
                    first_archived=r["first_archived"],
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

    def summary(self) -> dict:
        """The three numbers every check reports, without the heavier statistics queries."""
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "files_indexed": q("SELECT COUNT(*) FROM files WHERE index_version > 0 AND index_error IS NULL"),
            "pages": q("SELECT COUNT(*) FROM pages"),
            "last_listing_sync": self.get_meta("last_listing_sync"),
        }

    def source_counts(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT issuer || '/' || source, COUNT(*) FROM notices GROUP BY 1 ORDER BY 1")}

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
            "pages_with_text": q("SELECT COUNT(*) FROM pages WHERE text_source IN ('layer', 'ocr')"),
            "pages_ocr": q("SELECT COUNT(*) FROM pages WHERE text_source = 'ocr'"),
            "last_listing_sync": self.get_meta("last_listing_sync"),
            "last_download": self.get_meta("last_download"),
        }

    # -- withdrawals --------------------------------------------------------

    def mark_removed(self, issuer: str, sources: list[str], sync_time: str) -> int:
        """Notices of these sources that the latest complete sync did not list any more."""
        marks = ",".join("?" * len(sources))
        cur = self.conn.execute(
            f"UPDATE notices SET removed_on=? WHERE issuer=? AND source IN ({marks}) AND last_seen < ?"
            " AND removed_on IS NULL",
            (sync_time, issuer, *sources, sync_time),
        )
        return cur.rowcount

    def ok_fetches(self) -> list[sqlite3.Row]:
        """The current successful live fetch of every URL, with the validators needed to re-check it."""
        return list(self.conn.execute(
            "SELECT f.url, f.sha256, f.etag, f.last_modified FROM fetches f"
            " WHERE f.id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url AND f2.status = 'ok')"
            " AND f.origin = 'live' ORDER BY f.url"))

    # -- structured fields --------------------------------------------------

    FIELD_COLUMNS = ("ref_no", "ref_key", "doc_date", "subject", "signatory", "designation", "header_phash",
                     "header_ink")

    def save_fields(self, sha256: str, fields: dict) -> None:
        cols = self.FIELD_COLUMNS
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO fields(sha256, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' * len(cols))}, ?)"
            f" ON CONFLICT(sha256) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
            (sha256, *(fields.get(c) for c in cols), utcnow()),
        )

    def fields(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM fields WHERE sha256=?", (sha256,)).fetchone()

    def files_with_ref(self, ref_key: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT sha256 FROM fields WHERE ref_key=?", (ref_key,))]

    def header_hashes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT sha256, header_phash FROM fields WHERE header_phash IS NOT NULL AND header_ink > 0.002"))

    def files_without_fields(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT f.sha256, f.kind FROM files f LEFT JOIN fields x ON x.sha256 = f.sha256"
            " WHERE f.index_version > 0 AND f.index_error IS NULL AND x.sha256 IS NULL"))

    # -- known fakes --------------------------------------------------------

    def add_debunk(self, *, source: str, url: str, published: str | None, claim: str | None,
                   issuer: str | None = "ugc") -> int:
        row = self.conn.execute("SELECT id FROM debunks WHERE url=?", (url,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE debunks SET source=?, published=COALESCE(?, published), claim=COALESCE(?, claim) WHERE id=?",
                (source, published, claim, row["id"]))
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO debunks(issuer, source, url, published, claim, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (issuer, source, url, published, claim, utcnow()))
        return cur.lastrowid

    def add_fake(self, sha256: str, debunk_id: int, kind: str, pages: Iterable[dict], note: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO fakes(sha256, debunk_id, kind, added_at, note) VALUES (?, ?, ?, ?, ?)",
            (sha256, debunk_id, kind, utcnow(), note))
        self.conn.execute("DELETE FROM fake_pages WHERE sha256=?", (sha256,))
        for p in pages:
            self.conn.execute(
                "INSERT INTO fake_pages(sha256, page_no, text, text_source, n_shingles, minhash, phash, dhash, ink)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha256, p["page_no"], p["text"], p["text_source"], p["n_shingles"], p["minhash"], p["phash"],
                 p["dhash"], p["ink"]))

    def fake_pages(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT sha256, page_no, text, n_shingles, minhash, phash, ink FROM fake_pages ORDER BY sha256, page_no"))

    def debunks_for(self, sha256: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT d.* FROM fakes f JOIN debunks d ON d.id = f.debunk_id WHERE f.sha256=? ORDER BY d.published",
            (sha256,)))

    def known_fakes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT f.sha256, f.kind, f.note, d.source, d.url, d.published, d.claim FROM fakes f"
            " JOIN debunks d ON d.id = f.debunk_id ORDER BY d.published DESC, f.sha256"))

    # -- receipts -----------------------------------------------------------

    def save_receipt(self, rid: str, *, sha256: str, filename: str | None, verdict: str, summary: str,
                     payload: str) -> None:
        self.conn.execute(
            "INSERT INTO receipts(id, created_at, sha256, filename, verdict, summary, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", (rid, utcnow(), sha256, filename, verdict, summary, payload))

    def receipt(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()

    # -- Wayback Machine captures ------------------------------------------

    def save_capture(self, url: str, first: str | None, last: str | None, count: int) -> None:
        self.conn.execute(
            "INSERT INTO archive_captures(url, first_capture, last_capture, captures, checked_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET first_capture=excluded.first_capture,"
            " last_capture=excluded.last_capture, captures=excluded.captures, checked_at=excluded.checked_at",
            (url, first, last, count, utcnow()))

    # -- feeds and coverage -------------------------------------------------

    def newest_notices(self, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT n.id, n.title, n.published, n.source, n.first_seen, n.removed_on,"
            " (SELECT l.url FROM links l WHERE l.notice_id = n.id ORDER BY l.id LIMIT 1) AS url"
            " FROM notices n WHERE n.source != 'archive'"
            " ORDER BY n.first_seen DESC, n.published DESC, n.id DESC LIMIT ?"
        )
        return [dict(r) for r in self.conn.execute(sql, (limit,))]

    def coverage(self) -> dict:
        def q(sql: str):
            return self.conn.execute(sql).fetchone()[0]

        latest = ("SELECT status, origin FROM fetches f"
                  " WHERE id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)")
        by: dict = {}
        for r in self.conn.execute(f"SELECT status, origin, COUNT(*) FROM ({latest}) GROUP BY 1, 2"):
            by[(r[0], r[1])] = r[2]
        listed = q("SELECT COUNT(DISTINCT l.url) FROM links l JOIN notices n ON n.id = l.notice_id"
                   " WHERE n.source != 'archive'")
        ok_live = by.get(("ok", "live"), 0)
        ok_wayback = by.get(("ok", "wayback"), 0)
        no_layer = q("SELECT COUNT(*) FROM pages WHERE text_source != 'layer'")
        ocr_ok = q("SELECT COUNT(*) FROM pages WHERE text_source = 'ocr'")
        return {
            "listing_entries": q("SELECT COUNT(*) FROM notices WHERE source != 'archive'"),
            "listing_entries_removed": q("SELECT COUNT(*) FROM notices WHERE removed_on IS NOT NULL"),
            "linked_files": listed,
            "downloaded_live": ok_live,
            "recovered_from_wayback": ok_wayback,
            "dead": sum(v for (st, _), v in by.items() if st in ("dead", "not_document")),
            "too_large": sum(v for (st, _), v in by.items() if st == "too_large"),
            "errors": sum(v for (st, _), v in by.items() if st == "error"),
            "coverage_pct": round(100 * min(ok_live + ok_wayback, listed) / listed, 1) if listed else 0.0,
            "files_indexed": q("SELECT COUNT(*) FROM files WHERE index_version > 0 AND index_error IS NULL"),
            "index_errors": q("SELECT COUNT(*) FROM files WHERE index_error IS NOT NULL"),
            "pages": q("SELECT COUNT(*) FROM pages"),
            "pages_without_text_layer": no_layer,
            "pages_unreadable_text_layer": q("SELECT COUNT(*) FROM pages WHERE text_source = 'garbled'"),
            "pages_ocr": ocr_ok,
            "pages_still_without_text": no_layer - ocr_ok,
            "files_with_reference_number": q("SELECT COUNT(*) FROM fields WHERE ref_key IS NOT NULL"),
            "archived_only_documents": q("SELECT COUNT(*) FROM notices WHERE source = 'archive'"),
            "first_capture_known": q("SELECT COUNT(*) FROM archive_captures WHERE first_capture IS NOT NULL"),
            "known_fakes": q("SELECT COUNT(DISTINCT sha256) FROM fakes"),
            "debunks": q("SELECT COUNT(*) FROM debunks"),
            "last_listing_sync": self.get_meta("last_listing_sync"),
        }

    def commit(self) -> None:
        self.conn.commit()
