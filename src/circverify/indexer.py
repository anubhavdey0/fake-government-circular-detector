"""Extract text and fingerprints from every downloaded file."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .db import Database
from .extract import UnsupportedFile, iter_pages, pdf_info
from .fingerprint import page_fingerprint
from .ocr import load_ocr
from .store import BlobStore

# Bump when extraction or fingerprints change; older files get re-indexed.
# 2: text of pages with a /Rotate flag is extracted as if unrotated.
INDEX_VERSION = 2
# Text for every page up to this many; page images only for the first few
# pages, which is where forwarded circulars come from and keeps indexing fast.
MAX_TEXT_PAGES = 400
MAX_HASH_PAGES = 40


@dataclass
class IndexResult:
    sha256: str
    pages: list[dict] = field(default_factory=list)
    n_pages: int | None = None
    text_pages: int = 0
    ocr_pages: int = 0
    producer: str | None = None
    error: str | None = None


def index_document(data: bytes, sha256: str, kind: str, ocr=None) -> IndexResult:
    result = IndexResult(sha256)
    try:
        if kind == "pdf":
            info = pdf_info(data)
            result.n_pages = info.n_pages
            producer = info.metadata.get("Producer") or info.metadata.get("Creator")
            result.producer = producer[:200] if producer else None
        else:
            result.n_pages = 1
        for page in iter_pages(data, kind=kind, max_pages=MAX_TEXT_PAGES,
                               max_render_pages=MAX_HASH_PAGES, ocr=ocr):
            fp = page_fingerprint(page.text, page.image)
            result.pages.append({"page_no": page.number, "text": page.text,
                                 "text_source": page.text_source, **fp})
            result.text_pages += page.text_source != "none"
            result.ocr_pages += page.text_source == "ocr"
    except UnsupportedFile as exc:
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one broken PDF must not stop the run
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# Worker-process state, set once per process by the pool initializer.
_worker_ocr = None


def _init_worker(use_ocr: bool | None) -> None:
    global _worker_ocr
    _worker_ocr = load_ocr(use_ocr)


def _index_path(path: str, sha256: str, kind: str) -> IndexResult:
    return index_document(Path(path).read_bytes(), sha256, kind, _worker_ocr)


def _save(db: Database, res: IndexResult) -> None:
    db.replace_pages(
        res.sha256,
        res.pages,
        n_pages=res.n_pages,
        text_pages=res.text_pages,
        ocr_pages=res.ocr_pages,
        producer=res.producer,
        version=INDEX_VERSION,
        error=res.error,
    )


def index_pending(
    db: Database,
    store: BlobStore,
    *,
    workers: int | None = None,
    use_ocr: bool | None = None,
    reindex: bool = False,
    on_result: Callable[[IndexResult, int], None] | None = None,
) -> dict:
    rows = [r for r in db.files_to_index(INDEX_VERSION, reindex=reindex) if store.has(r["sha256"])]
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    done = errors = 0
    if workers == 1 or len(rows) <= 2:
        ocr = load_ocr(use_ocr)
        for r in rows:
            res = index_document(store.get(r["sha256"]), r["sha256"], r["kind"], ocr)
            _save(db, res)
            done += 1
            errors += res.error is not None
            if on_result:
                on_result(res, len(rows))
            if done % 20 == 0:
                db.commit()
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(use_ocr,)) as pool:
            futures = [pool.submit(_index_path, str(store.path(r["sha256"])), r["sha256"], r["kind"])
                       for r in rows]
            for fut in as_completed(futures):
                res = fut.result()
                _save(db, res)
                done += 1
                errors += res.error is not None
                if on_result:
                    on_result(res, len(rows))
                if done % 20 == 0:
                    db.commit()
    db.commit()
    return {"indexed": done, "errors": errors}
