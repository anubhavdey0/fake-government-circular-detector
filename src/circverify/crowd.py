"""Documents people report as fake: a review queue that slowly becomes a real dataset.

The tool calls something fake only when it matches a public debunk. A report does not change
that. It is a lead for a maintainer, who can:

* promote it to a known fake by attaching the URL of a public debunk (the same safety check as
  ``circverify fakes add`` applies: a file that matches an official document is refused);
* dismiss it;
* export the queue, files and notes, as a dataset of forgeries that actually circulated.

A file that verifies as an official document cannot be reported at all.
"""

from __future__ import annotations

import csv
import re
import secrets
import shutil
from pathlib import Path

from .db import Database
from .extract import sniff_kind
from .store import BlobStore

MAX_TEXT = 500
_URL = re.compile(r"^https?://[^\s<>\"']{4,300}$", re.I)
_EXT = {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg", "webp": ".webp", "gif": ".gif", "tiff": ".tif", "text": ".txt"}


def _clean(value: str | None, limit: int = MAX_TEXT) -> str | None:
    if not value:
        return None
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value).strip()
    return value[:limit] or None


def _clean_url(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _URL.match(value) else None


def create_report(db: Database, store: BlobStore, verifier, *, data: bytes | None = None, text: str | None = None,
                  filename: str | None = None, channel: str = "web", seen_where: str | None = None,
                  note: str | None = None, debunk_url: str | None = None) -> tuple[str | None, str]:
    """File a report. Returns ``(report id, status)``; the id is None when the report is refused."""
    from .matcher import UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT

    if data is None and not (text and text.strip()):
        return None, "refused: nothing to report"
    if data is not None:
        kind = sniff_kind(data[:2048])
        if kind is None:
            return None, "refused: not a PDF or image"
        res = verifier.verify(data, filename)
        blob = data
    else:
        kind = "text"
        text = text.strip()
        res = verifier.verify_text(text)
        blob = text.encode("utf-8")
    if res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT):
        title = res.match.primary.title if res.match and res.match.primary else "an official document"
        return None, f"refused: this is the official “{title}”"
    if res.verdict == UNREADABLE:
        return None, "refused: the file could not be read"
    seen_where, note, debunk_url = _clean(seen_where), _clean(note), _clean_url(debunk_url)
    open_report = db.open_report_for(res.sha256)
    if open_report is not None:
        db.count_report(open_report["id"], seen_where=seen_where, note=note, debunk_url=debunk_url)
        db.commit()
        return open_report["id"], "counted"
    store.put_bytes(blob)
    rid = secrets.token_urlsafe(8)
    db.add_report(rid, sha256=res.sha256, filename=_clean(filename, 200), kind=kind, verdict=res.verdict,
                  seen_where=seen_where, note=note, debunk_url=debunk_url, channel=channel)
    db.commit()
    return rid, "added"


def promote(db: Database, store: BlobStore, rid: str, *, url: str, source: str, published: str | None = None,
            claim: str | None = None, verifier=None) -> tuple[str | None, str]:
    """Turn a report into a known fake, backed by a public debunk."""
    from .fakes import register_fake

    row = db.report(rid)
    if row is None:
        return None, "no such report"
    if row["kind"] == "text":
        return None, "a pasted-text report has no file to register; add the image or PDF with `circverify fakes add`"
    if not _clean_url(url):
        return None, "the debunk must be a public http(s) URL"
    sha, status = register_fake(db, store, store.get(row["sha256"]), source=source, url=url, published=published,
                                claim=claim or row["note"], verifier=verifier, note=f"reported ({row['count']}x)")
    if sha:
        db.set_report_status(rid, "promoted", f"debunk: {url}")
        db.commit()
    return sha, status


def dismiss(db: Database, rid: str, note: str | None = None) -> bool:
    if db.report(rid) is None:
        return False
    db.set_report_status(rid, "dismissed", _clean(note))
    db.commit()
    return True


COLUMNS = ("id", "created_at", "updated_at", "status", "count", "verdict", "kind", "filename", "sha256", "channel",
           "seen_where", "note", "debunk_url", "review_note")


def export(db: Database, store: BlobStore, out: Path, status: str | None = None) -> int:
    """Copy reported files to ``out`` with a ``reports.csv`` describing each one."""
    out.mkdir(parents=True, exist_ok=True)
    rows = db.reports(status, limit=100_000)
    with (out / "reports.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([*COLUMNS, "file"])
        for r in rows:
            name = f"{r['id']}{_EXT.get(r['kind'] or '', '')}"
            if store.has(r["sha256"]):
                shutil.copyfile(store.path(r["sha256"]), out / name)
            w.writerow([*(r[c] for c in COLUMNS), name])
    return len(rows)
