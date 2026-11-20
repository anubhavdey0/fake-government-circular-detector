"""Extract text, fingerprints and structured fields from every downloaded file."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .db import Database
from .extract import UnsupportedFile, iter_pages, pdf_info
from .extract import HASH_WIDTH, OCR_WIDTH, open_pdf, _render  # noqa: PLC2701 - internal helpers
from .fields import extract_fields
from .fingerprint import header_fingerprint, page_fingerprint
from .layout import dumps as layout_dumps
from .layout import layout_features
from .links import text_domains
from .ocr import load_ocr
from .store import BlobStore

# Bump when extraction or fingerprints change; older files get re-indexed.
# 2: text of pages with a /Rotate flag is extracted as if unrotated.
INDEX_VERSION = 2
# Text for every page up to this many; page images only for the first few
# pages, which is where forwarded circulars come from and keeps indexing fast.
MAX_TEXT_PAGES = 400
MAX_HASH_PAGES = 40
# OCR is slow, so by default only the first pages of a scan get it.
DEFAULT_OCR_PAGES = 2


@dataclass
class IndexResult:
    sha256: str
    pages: list[dict] = field(default_factory=list)
    n_pages: int | None = None
    text_pages: int = 0
    ocr_pages: int = 0
    producer: str | None = None
    fields: dict = field(default_factory=dict)
    error: str | None = None


def _fields_for(pages_text: list[str], header_image) -> dict:
    f = extract_fields("\n".join(pages_text[:2]))
    if header_image is not None:
        f["header_phash"], f["header_ink"] = header_fingerprint(header_image)
    f["domains"] = " ".join(sorted(text_domains("\n".join(pages_text))))
    f["layout"] = layout_dumps(layout_features(header_image)) if header_image is not None else ""
    return f


def index_document(data: bytes, sha256: str, kind: str, ocr=None, ocr_pages: int = DEFAULT_OCR_PAGES) -> IndexResult:
    result = IndexResult(sha256)
    try:
        if kind == "pdf":
            info = pdf_info(data)
            result.n_pages = info.n_pages
            producer = info.metadata.get("Producer") or info.metadata.get("Creator")
            result.producer = producer[:200] if producer else None
        else:
            result.n_pages = 1
        first_image = None
        for page in iter_pages(data, kind=kind, max_pages=MAX_TEXT_PAGES, max_render_pages=MAX_HASH_PAGES,
                               ocr=ocr if ocr is not None else None, ocr_pages=ocr_pages):
            if page.number == 1:
                first_image = page.image
            fp = page_fingerprint(page.text, page.image)
            result.pages.append({"page_no": page.number, "text": str(page.text),
                                 "text_source": page.text_source, "ocr_conf": getattr(page.text, "confidence", None),
                                 **fp})
            result.text_pages += page.text_source in ("layer", "ocr")
            result.ocr_pages += page.text_source == "ocr"
        result.fields = _fields_for([p["text"] for p in result.pages], first_image)
    except UnsupportedFile as exc:
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one broken PDF must not stop the run
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# Worker-process state, set once per process by the pool initializer.
_worker_ocr = None


def _init_worker(use_ocr: bool | None) -> None:
    global _worker_ocr  # noqa: PLW0603 - one OCR engine per worker process
    _worker_ocr = load_ocr(use_ocr) if use_ocr else None


def _index_path(path: str, sha256: str, kind: str, ocr_pages: int) -> IndexResult:
    return index_document(Path(path).read_bytes(), sha256, kind, _worker_ocr, ocr_pages)


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
    if res.error is None:
        db.save_fields(res.sha256, res.fields)


def _workers(workers: int | None, use_ocr: bool | None) -> int:
    if workers:
        return workers
    cpus = os.cpu_count() or 2
    # Each OCR worker loads its own models and runs several threads.
    return max(1, cpus // 3) if use_ocr else max(1, cpus - 1)


def index_pending(
    db: Database,
    store: BlobStore,
    *,
    workers: int | None = None,
    use_ocr: bool | None = None,
    ocr_pages: int = DEFAULT_OCR_PAGES,
    reindex: bool = False,
    on_result: Callable[[IndexResult, int], None] | None = None,
) -> dict:
    rows = [r for r in db.files_to_index(INDEX_VERSION, reindex=reindex) if store.has(r["sha256"])]
    workers = _workers(workers, use_ocr)
    done = errors = 0
    if workers == 1 or len(rows) <= 2:
        ocr = load_ocr(use_ocr) if use_ocr else None
        for r in rows:
            res = index_document(store.get(r["sha256"]), r["sha256"], r["kind"], ocr, ocr_pages)
            _save(db, res)
            done += 1
            errors += res.error is not None
            if on_result:
                on_result(res, len(rows))
            if done % 20 == 0:
                db.commit()
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(use_ocr,)) as pool:
            futures = [pool.submit(_index_path, str(store.path(r["sha256"])), r["sha256"], r["kind"], ocr_pages)
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


# -- enrichment of files already indexed ----------------------------------------------

@dataclass
class EnrichResult:
    sha256: str
    fields: dict = field(default_factory=dict)
    ocr_pages: list[dict] = field(default_factory=list)  # page dicts with new OCR text
    error: str | None = None


def enrich_document(data: bytes, sha256: str, kind: str, texts: dict[int, str], ocr_targets: list[int],
                    ocr=None) -> EnrichResult:
    """Fields and letterhead fingerprint for an indexed file, plus OCR of the listed pages."""
    res = EnrichResult(sha256)
    try:
        if kind == "pdf":
            doc = open_pdf(data)
            try:
                page = doc[0]
                try:
                    header = _render(page, HASH_WIDTH)
                finally:
                    page.close()
                if ocr is not None:
                    for no in ocr_targets:
                        if no > len(doc):
                            continue
                        pg = doc[no - 1]
                        try:
                            text = ocr(_render(pg, OCR_WIDTH))
                            image = _render(pg, HASH_WIDTH)
                        finally:
                            pg.close()
                        if text.strip():
                            texts[no] = text
                            res.ocr_pages.append({"page_no": no, "text": str(text), "text_source": "ocr",
                                                  "ocr_conf": getattr(text, "confidence", None),
                                                  **page_fingerprint(text, image)})
            finally:
                doc.close()
        else:
            header = next(iter_pages(data, kind=kind, render_width=HASH_WIDTH)).image
        res.fields = _fields_for([texts.get(n, "") for n in sorted(texts)], header)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def _enrich_path(path: str, sha256: str, kind: str, texts: dict[int, str], targets: list[int]) -> EnrichResult:
    return enrich_document(Path(path).read_bytes(), sha256, kind, texts, targets, _worker_ocr)


def mark_garbled(db: Database) -> int:
    """Flag existing pages whose text layer cannot be read (see ``extract.text_quality``)."""
    from .extract import GARBLED_BELOW, text_quality

    marked = 0
    rows = db.conn.execute("SELECT sha256, page_no, text FROM pages WHERE text_source = 'layer'").fetchall()
    for sha, no, text in rows:
        q = text_quality(text)
        if q is not None and q < GARBLED_BELOW:
            db.update_page_text(sha, {"page_no": no, "text": "", "text_source": "garbled", "n_shingles": 0,
                                      "minhash": None})
            marked += 1
    db.commit()
    return marked


def enrich_pending(
    db: Database,
    store: BlobStore,
    *,
    workers: int | None = None,
    use_ocr: bool = False,
    ocr_pages: int = DEFAULT_OCR_PAGES,
    limit: int | None = None,
    redo_ocr: bool = False,
    on_result: Callable[[EnrichResult, int], None] | None = None,
) -> dict:
    """Add fields to files that lack them and, with ``use_ocr``, OCR their first text-less pages.

    ``redo_ocr`` reads pages already OCR'd once more, with the current engine: after installing
    Tesseract with the Hindi pack, say, pages first read by an English-only engine."""
    jobs: dict[str, tuple[str, list[int]]] = {r["sha256"]: (r["kind"], []) for r in db.files_without_fields()}
    if use_ocr:
        sources = ("none", "garbled", "ocr") if redo_ocr else ("none", "garbled")
        marks = ",".join("?" * len(sources))
        for r in db.conn.execute(
            "SELECT p.sha256, f.kind, p.page_no FROM pages p JOIN files f ON f.sha256 = p.sha256"
            f" WHERE p.text_source IN ({marks}) AND p.page_no <= ? AND f.kind = 'pdf'"
            " ORDER BY p.sha256, p.page_no",
            (*sources, ocr_pages),
        ):
            jobs.setdefault(r["sha256"], (r["kind"], []))[1].append(r["page_no"])
    items = [(sha, kind, targets) for sha, (kind, targets) in jobs.items() if store.has(sha)]
    if limit is not None:
        items = items[:limit]
    workers = _workers(workers, use_ocr)
    done = errors = ocr_done = 0

    def texts_for(sha: str) -> dict[int, str]:
        return {no: t for no, t in db.conn.execute(
            "SELECT page_no, text FROM pages WHERE sha256=? AND text_source IN ('layer', 'ocr') ORDER BY page_no",
            (sha,))}

    def save(res: EnrichResult) -> None:
        nonlocal done, errors, ocr_done
        done += 1
        if res.error:
            errors += 1
        else:
            before = texts_for(res.sha256) if redo_ocr else {}
            for p in res.ocr_pages:
                # A second reading far shorter than the first is a failed read, not a better one.
                if len(p.get("text") or "") * 2 < len(before.get(p["page_no"], "")):
                    continue
                db.update_page_text(res.sha256, p)
            ocr_done += len(res.ocr_pages)
            db.save_fields(res.sha256, res.fields, keep_existing=True)
        if on_result:
            on_result(res, len(items))
        if done % 20 == 0:
            db.commit()

    if workers == 1 or len(items) <= 2:
        ocr = load_ocr(True) if use_ocr else None
        for sha, kind, targets in items:
            save(enrich_document(store.get(sha), sha, kind, texts_for(sha), targets, ocr))
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(use_ocr,)) as pool:
            futures = [pool.submit(_enrich_path, str(store.path(sha)), sha, kind, texts_for(sha), targets)
                       for sha, kind, targets in items]
            for fut in as_completed(futures):
                save(fut.result())
    db.commit()
    return {"files": done, "errors": errors, "ocr_pages": ocr_done}
