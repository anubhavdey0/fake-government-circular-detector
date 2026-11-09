from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

from pdfgen import make_pdf  # noqa: E402

from circverify.db import Database  # noqa: E402
from circverify.extract import render_pdf_page  # noqa: E402
from circverify.indexer import index_pending  # noqa: E402
from circverify.issuers.base import Link, Notice  # noqa: E402
from circverify.store import BlobStore  # noqa: E402

# A made-up circular in the usual shape. Line 8 carries the deadline.
OFFICIAL = [
    "University Grants Commission",
    "Bahadur Shah Zafar Marg, New Delhi - 110002",
    "F.No. 1-15/2026 (CPP-II)                              30 October 2026",
    "To",
    "The Vice-Chancellors of all Universities",
    "Subject: Extension of last date for submission of fellowship applications",
    "Sir/Madam,",
    "The Commission has decided to extend the last date for online submission",
    "of applications under the fellowship scheme to 15 November 2026.",
    "Applications must be submitted only through the official portal",
    "www.ugc.gov.in. No application fee is payable at any stage.",
    "Universities are requested to give wide publicity to this notice",
    "among students and faculty members in their institutions.",
    "Yours faithfully,",
    "(Secretary)",
]
DEADLINE_LINE = 8

OTHER = [
    "University Grants Commission",
    "Bahadur Shah Zafar Marg, New Delhi - 110002",
    "F.No. 9-2/2026 (Sports)                                 12 August 2026",
    "Subject: Inter-university athletics meet at Bhopal",
    "The Commission is pleased to announce that the annual athletics meet",
    "will be hosted by the host university between 2 and 6 December 2026.",
    "Teams should register with the organising secretary by 1 November 2026.",
    "Travel support will be provided as per existing norms.",
]

UNRELATED = [
    "Office of the District Collector",
    "Public notice regarding water supply interruption in ward 14",
    "Water supply will remain suspended for maintenance of the main pipeline",
    "from 6 a.m. to 6 p.m. on Sunday. Residents are requested to store water",
    "in advance and cooperate with the municipal staff during the work.",
]


def png_bytes(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def add_official(db: Database, store: BlobStore, data: bytes, title: str, record_id: str,
                 published: str = "2026-10-30") -> str:
    url = f"https://www.ugc.gov.in/pdfnews/{record_id}.pdf"
    db.upsert_notice(Notice("ugc", "notices", record_id, title, published, [Link(url)],
                            "https://www.ugc.gov.in/Notices"))
    sha = store.put_bytes(data)
    db.record_fetch(url, status="ok", http_status=200, final_url=url, sha256=sha)
    db.add_file(sha, len(data), "pdf")
    db.commit()
    return sha


@pytest.fixture()
def official_pdf() -> bytes:
    return make_pdf([OFFICIAL], producer="Office scanner suite")


@pytest.fixture()
def index(tmp_path, official_pdf):
    """An index holding the made-up circular plus one other notice."""
    db = Database(tmp_path / "index.sqlite3")
    store = BlobStore(tmp_path)
    sha = add_official(db, store, official_pdf, "Extension of last date for fellowship applications", "1001")
    add_official(db, store, make_pdf([OTHER]), "Inter-university athletics meet", "1002", "2026-08-12")
    index_pending(db, store, workers=1, use_ocr=False)
    yield db, store, sha
    db.close()


@pytest.fixture()
def official_render(official_pdf) -> Image.Image:
    return render_pdf_page(official_pdf, 1, 1000)
