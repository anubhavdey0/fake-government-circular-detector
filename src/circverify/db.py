"""SQLite index: notices, their links, fetch history, files and per-page fingerprints."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .issuers.base import Notice

SCHEMA_VERSION = 7

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
    ocr_conf    REAL,  -- mean OCR confidence (0-1) for pages read by OCR
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
    updated_at   TEXT NOT NULL,
    domains      TEXT,  -- registrable domains the document mentions, space-separated
    layout       TEXT   -- first-page layout measurements (JSON), see layout.py
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

-- Documents people reported as fake. A review queue, never a verdict: a report becomes a
-- known fake only when a maintainer attaches a public debunk (see crowd.py).
CREATE TABLE IF NOT EXISTS reports(
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    filename    TEXT,
    kind        TEXT,
    verdict     TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    seen_where  TEXT,
    note        TEXT,
    debunk_url  TEXT,
    channel     TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS reports_sha ON reports(sha256, status);

-- Later documents that correct, amend, extend, replace or withdraw earlier ones (relations.py).
CREATE TABLE IF NOT EXISTS relations(
    from_sha  TEXT NOT NULL,  -- the later document
    to_sha    TEXT NOT NULL,  -- the earlier document it updates
    kind      TEXT NOT NULL,  -- withdraws | supersedes | corrects | amends | extends | follows
    evidence  TEXT,
    PRIMARY KEY (from_sha, to_sha)
);
CREATE INDEX IF NOT EXISTS relations_to ON relations(to_sha);

-- When the Wayback Machine first and last saw each official URL.
CREATE TABLE IF NOT EXISTS archive_captures(
    url           TEXT PRIMARY KEY,
    first_capture TEXT,
    last_capture  TEXT,
    captures      INTEGER,
    checked_at    TEXT NOT NULL,
    snapshot      TEXT  -- raw bytes of the first capture that is the document itself
);
"""

# Columns added after version 1, applied in place to existing indexes.
_ADDED_COLUMNS = {
    "notices": [("removed_on", "TEXT")],
    "fetches": [("etag", "TEXT"), ("last_modified", "TEXT")],
    "archive_captures": [("snapshot", "TEXT")],
    "fields": [("domains", "TEXT"), ("layout", "TEXT")],
    "pages": [("ocr_conf", "REAL")],
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
        listed = "SELECT l2.url FROM links l2 JOIN notices n2 ON n2.id = l2.notice_id WHERE n2.source != 'archive'"
        if refresh:
            sql = f"SELECT DISTINCT url FROM links WHERE url IN ({listed}) ORDER BY url"
        else:
            retry = ("'error'", "'dead'", "'not_document'") if retry_failed else ("'error'",)
            sql = f"""
                SELECT DISTINCT l.url FROM links l
                LEFT JOIN (
                    SELECT url, status FROM fetches f
                    WHERE id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)
                ) last ON last.url = l.url
                WHERE (last.url IS NULL OR last.status IN ({", ".join(retry)})) AND l.url IN ({listed})
                ORDER BY l.url
            """
        return [r["url"] for r in self.conn.execute(sql)]

    def dead_urls(self) -> list[str]:
        sql = """
            SELECT f.url FROM fetches f
            WHERE f.id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)
              AND f.status IN ('dead', 'not_document')
              AND f.url IN (SELECT l2.url FROM links l2 JOIN notices n2 ON n2.id = l2.notice_id WHERE n2.source != 'archive')
            ORDER BY f.url
        """
        return [r["url"] for r in self.conn.execute(sql)]

    def archived_urls(self) -> set[str]:
        """URLs the issuer lists, plus archive-only URLs whose file has been downloaded."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT l.url FROM links l JOIN notices n ON n.id = l.notice_id"
            " WHERE n.source != 'archive'"
            " OR EXISTS (SELECT 1 FROM fetches f WHERE f.url = l.url AND f.status = 'ok')")}

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
                " phash, dhash, ink, ocr_conf) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    p.get("ocr_conf"),
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
        c.execute("UPDATE pages SET text=?, text_source=?, n_shingles=?, minhash=?, ocr_conf=? WHERE id=?",
                  (page["text"], page["text_source"], page["n_shingles"], page["minhash"], page.get("ocr_conf"),
                   row["id"]))
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
        rows = list(self.conn.execute(sql, (query, limit * 5)))
        # Documents UGC lists today come first; archived ones (long annual reports that
        # mention everything) follow. Within each group, best text match first.
        shas = list({r["sha256"] for r in rows})
        listed: set[str] = set()
        if shas:
            marks_q = ",".join("?" * len(shas))
            listed = {r[0] for r in self.conn.execute(
                f"SELECT DISTINCT f.sha256 FROM fetches f JOIN links l ON l.url = f.url"
                f" JOIN notices n ON n.id = l.notice_id WHERE n.source != 'archive' AND f.sha256 IN ({marks_q})",
                shas)}
        rows.sort(key=lambda r: (r["sha256"] not in listed, r["score"]))
        return rows[:limit]

    def browse(self, *, source: str | None = None, date_from: str | None = None, date_to: str | None = None,
               status: str = "listed", ref_key: str | None = None, limit: int = 30,
               offset: int = 0) -> tuple[list[dict], int]:
        """The public registry: listing entries, newest first, with filters. Returns (rows, total)."""
        where, params = ["1=1"], []
        if status == "listed":
            where.append("n.source != 'archive' AND n.removed_on IS NULL")
        elif status == "withdrawn":
            where.append("n.removed_on IS NOT NULL")
        elif status == "archived":
            where.append("n.source = 'archive'")
        if source:
            where.append("n.source = ?")
            params.append(source)
        if date_from:
            where.append("n.published >= ?")
            params.append(date_from)
        if date_to:
            where.append("n.published <= ?")
            params.append(date_to)
        if ref_key:
            # Exact reference first; "1-15/2026" also finds "1-15/2026 (CPP-II)".
            exact = self.conn.execute("SELECT 1 FROM fields WHERE ref_key = ? LIMIT 1", (ref_key,)).fetchone()
            op = "x.ref_key = ?" if exact else "x.ref_key LIKE ? || '%'"
            where.append("EXISTS (SELECT 1 FROM links l JOIN fetches f ON f.url = l.url AND f.status = 'ok'"
                         f" JOIN fields x ON x.sha256 = f.sha256 WHERE l.notice_id = n.id AND {op})")
            params.append(ref_key)
        cond = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM notices n WHERE {cond}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT n.id, n.title, n.published, n.source, n.removed_on,
                      (SELECT l.url FROM links l WHERE l.notice_id = n.id ORDER BY l.id LIMIT 1) AS url,
                      (SELECT f.sha256 FROM links l JOIN fetches f ON f.url = l.url AND f.status = 'ok'
                        WHERE l.notice_id = n.id ORDER BY l.id, f.id DESC LIMIT 1) AS sha256
               FROM notices n WHERE {cond}
               ORDER BY n.published IS NULL, n.published DESC, n.id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
        return [dict(r) for r in rows], total

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
        """Listing entries per source; archived documents count only once their file is downloaded."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT n.issuer || '/' || n.source, COUNT(*) FROM notices n WHERE n.source != 'archive'"
            " OR EXISTS (SELECT 1 FROM links l JOIN fetches f ON f.url = l.url"
            "            WHERE l.notice_id = n.id AND f.status = 'ok') GROUP BY 1 ORDER BY 1")}

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
                     "header_ink", "domains", "layout")

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
        """Indexed files whose fields are missing or predate the domain and layout columns."""
        return list(self.conn.execute(
            "SELECT f.sha256, f.kind FROM files f LEFT JOIN fields x ON x.sha256 = f.sha256"
            " WHERE f.index_version > 0 AND f.index_error IS NULL"
            " AND (x.sha256 IS NULL OR x.domains IS NULL OR x.layout IS NULL)"))

    # -- the issuer's own habits (domains, tools, layouts) ------------------

    def known_domains(self) -> dict[str, int]:
        """In how many of the issuer's documents each registrable domain appears."""
        counts: dict[str, int] = {}
        for (raw,) in self.conn.execute("SELECT domains FROM fields WHERE domains IS NOT NULL AND domains != ''"):
            for d in set(raw.split()):
                if len(d.split(".")[0]) <= 1:  # "B.Com", "M.Com": degree names, not domains
                    continue
                counts[d] = counts.get(d, 0) + 1
        return counts

    def producer_counts(self) -> tuple[dict[str, int], int]:
        """Tool families that wrote the issuer's own PDFs, and how many PDFs that covers."""
        from .forensics import tool_family

        counts: dict[str, int] = {}
        total = 0
        for (producer,) in self.conn.execute("SELECT producer FROM files WHERE kind = 'pdf' AND index_version > 0"):
            total += 1
            fam = tool_family(producer)
            if fam:
                counts[fam] = counts.get(fam, 0) + 1
        return counts, total

    def layout_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT layout FROM fields WHERE layout IS NOT NULL AND layout != ''"))

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

    # -- relations between documents ----------------------------------------

    def relation_inputs(self) -> list[sqlite3.Row]:
        """Every indexed file with its earliest listing date, that listing's title, reference and subject."""
        return list(self.conn.execute("""
            SELECT f.sha256, x.ref_key, x.subject,
                   (SELECT n.title FROM fetches fe JOIN links l ON l.url = fe.url JOIN notices n ON n.id = l.notice_id
                     WHERE fe.sha256 = f.sha256 AND fe.status = 'ok'
                     ORDER BY n.published IS NULL, n.published LIMIT 1) AS title,
                   (SELECT MIN(n.published) FROM fetches fe JOIN links l ON l.url = fe.url
                     JOIN notices n ON n.id = l.notice_id WHERE fe.sha256 = f.sha256 AND fe.status = 'ok') AS published
            FROM files f LEFT JOIN fields x ON x.sha256 = f.sha256
            WHERE f.index_version > 0 AND f.index_error IS NULL"""))

    def first_pages_text(self, sha256: str, n: int = 3) -> str:
        return "\n".join(t for (t,) in self.conn.execute(
            "SELECT text FROM pages WHERE sha256 = ? AND page_no <= ? AND text_source IN ('layer', 'ocr')"
            " ORDER BY page_no", (sha256, n)) if t)

    def save_relations(self, rows: list[tuple[str, str, str, str]]) -> None:
        self.conn.execute("DELETE FROM relations")
        self.conn.executemany("INSERT OR REPLACE INTO relations(from_sha, to_sha, kind, evidence) VALUES (?, ?, ?, ?)",
                              rows)
        self.commit()

    def _related(self, sql: str, sha256: str) -> list[dict]:
        out = []
        for r in self.conn.execute(sql, (sha256,)):
            prov = self.provenance(r["other"])
            if not prov:
                continue
            p = min(prov, key=lambda x: x.published or "9999")
            out.append({"sha256": r["other"], "kind": r["kind"], "evidence": r["evidence"], "title": p.title,
                        "published": p.published, "url": p.url})
        return sorted(out, key=lambda d: d["published"] or "")

    def later_documents(self, sha256: str, *, include_follows: bool = False) -> list[dict]:
        """Documents that may update this one (correct, amend, extend, replace or withdraw it)."""
        kinds = "" if include_follows else " AND kind != 'follows'"
        return self._related(f"SELECT from_sha AS other, kind, evidence FROM relations WHERE to_sha = ?{kinds}",
                             sha256)

    def earlier_documents(self, sha256: str) -> list[dict]:
        """Documents this one updates."""
        return self._related("SELECT to_sha AS other, kind, evidence FROM relations WHERE from_sha = ?"
                             " AND kind != 'follows'", sha256)

    # -- crowd reports ------------------------------------------------------

    def open_report_for(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reports WHERE sha256=? AND status='new'", (sha256,)).fetchone()

    def add_report(self, rid: str, *, sha256: str, filename: str | None, kind: str | None, verdict: str,
                   seen_where: str | None, note: str | None, debunk_url: str | None, channel: str) -> None:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO reports(id, created_at, updated_at, sha256, filename, kind, verdict, seen_where, note,"
            " debunk_url, channel) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, now, now, sha256, filename, kind, verdict, seen_where, note, debunk_url, channel))

    def count_report(self, rid: str, *, seen_where: str | None, note: str | None, debunk_url: str | None) -> None:
        row = self.conn.execute("SELECT seen_where, note, debunk_url FROM reports WHERE id=?", (rid,)).fetchone()

        def merged(old: str | None, new: str | None) -> str | None:
            if not new or (old and new in old):
                return old
            return (f"{old}\n{new}" if old else new)[:2000]

        self.conn.execute(
            "UPDATE reports SET count = count + 1, updated_at=?, seen_where=?, note=?, debunk_url=? WHERE id=?",
            (utcnow(), merged(row["seen_where"], seen_where), merged(row["note"], note),
             row["debunk_url"] or debunk_url, rid))

    def report(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()

    def reports(self, status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if status:
            return list(self.conn.execute(
                "SELECT * FROM reports WHERE status=? ORDER BY count DESC, updated_at DESC LIMIT ?", (status, limit)))
        return list(self.conn.execute("SELECT * FROM reports ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def set_report_status(self, rid: str, status: str, review_note: str | None = None) -> None:
        self.conn.execute("UPDATE reports SET status=?, review_note=?, updated_at=? WHERE id=?",
                          (status, review_note, utcnow(), rid))

    # -- receipts -----------------------------------------------------------

    def save_receipt(self, rid: str, *, sha256: str, filename: str | None, verdict: str, summary: str,
                     payload: str) -> None:
        self.conn.execute(
            "INSERT INTO receipts(id, created_at, sha256, filename, verdict, summary, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", (rid, utcnow(), sha256, filename, verdict, summary, payload))

    def receipt(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()

    # -- Wayback Machine captures ------------------------------------------

    def save_capture(self, url: str, first: str | None, last: str | None, count: int,
                     snapshot: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO archive_captures(url, first_capture, last_capture, captures, checked_at, snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET first_capture=excluded.first_capture,"
            " last_capture=excluded.last_capture, captures=excluded.captures, checked_at=excluded.checked_at,"
            " snapshot=excluded.snapshot",
            (url, first, last, count, utcnow(), snapshot))

    def capture_snapshots(self, urls: list[str]) -> dict[str, str]:
        """Known Wayback Machine captures of ``urls`` that are the document itself."""
        wanted = set(urls)
        rows = self.conn.execute("SELECT url, snapshot FROM archive_captures WHERE snapshot IS NOT NULL")
        return {u: s for u, s in rows if u in wanted}

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
                  " WHERE id = (SELECT MAX(id) FROM fetches f2 WHERE f2.url = f.url)"
                  " AND f.url IN (SELECT l2.url FROM links l2 JOIN notices n2 ON n2.id = l2.notice_id WHERE n2.source != 'archive')")
        by: dict = {}
        for r in self.conn.execute(f"SELECT status, origin, COUNT(*) FROM ({latest}) GROUP BY 1, 2"):
            by[(r[0], r[1])] = r[2]
        listed_sql = "SELECT l2.url FROM links l2 JOIN notices n2 ON n2.id = l2.notice_id WHERE n2.source != 'archive'"
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
            "ocr_confidence": q("SELECT ROUND(AVG(ocr_conf), 3) FROM pages WHERE text_source = 'ocr'"),
            "pages_still_without_text": no_layer - ocr_ok,
            "files_with_reference_number": q("SELECT COUNT(*) FROM fields WHERE ref_key IS NOT NULL"),
            "archived_only_documents": q(
                "SELECT COUNT(DISTINCT l.url) FROM links l JOIN notices n ON n.id = l.notice_id"
                " WHERE n.source = 'archive' AND l.url NOT IN (" + listed_sql + ")"
                " AND EXISTS (SELECT 1 FROM fetches f WHERE f.url = l.url AND f.status = 'ok')"),
            "first_capture_known": q(
                "SELECT COUNT(*) FROM archive_captures a WHERE a.first_capture IS NOT NULL AND (a.url IN ("
                + listed_sql + ") OR EXISTS (SELECT 1 FROM fetches f WHERE f.url = a.url AND f.status = 'ok'))"),
            "known_fakes": q("SELECT COUNT(DISTINCT sha256) FROM fakes"),
            "debunks": q("SELECT COUNT(*) FROM debunks"),
            "reports_new": q("SELECT COUNT(*) FROM reports WHERE status = 'new'"),
            "reports_total": q("SELECT COUNT(*) FROM reports"),
            "receipts": q("SELECT COUNT(*) FROM receipts"),
            "relations": q("SELECT COUNT(*) FROM relations WHERE kind != 'follows'"),
            "withdrawn_or_updated": q("SELECT COUNT(DISTINCT to_sha) FROM relations WHERE kind != 'follows'"),
            "last_listing_sync": self.get_meta("last_listing_sync"),
        }

    def commit(self) -> None:
        self.conn.commit()
