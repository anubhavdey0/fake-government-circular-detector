"""The local web app: ``circverify serve``.

Routes: ``/`` check page, ``/verify`` (form post; ``?fragment=1`` for the
in-page result), ``/report`` (same check, downloadable HTML), ``/search``,
``/thumb/<sha256>`` (first page of an indexed official file), ``/api/verify``
and ``/api/stats`` (JSON).
"""

from __future__ import annotations

import io
import re
import threading
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import Database
from .extract import render_pdf_page
from .htmlreport import render_not_found, render_page, render_report, render_result, render_search, snippet_html
from .icons import favicon_svg
from .matcher import VerificationResult, Verifier
from .store import BlobStore

MAX_UPLOAD = 30 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_WORD = re.compile(r"[\wऀ-ॣ०-෿]+")


def thumb_url(sha: str, width: int) -> str:
    return f"/thumb/{sha}?w={width}"


class _State:
    """One verifier, rebuilt when the index grows; one PDF operation at a time (pdfium is not thread-safe)."""

    def __init__(self, data_dir: Path):
        self.db = Database(Path(data_dir) / "index.sqlite3", check_same_thread=False)
        self.store = BlobStore(Path(data_dir))
        self.lock = threading.Lock()
        self._verifier: Verifier | None = None
        self._indexed = -1
        self._thumb = lru_cache(maxsize=256)(self._render_thumb)

    def info(self) -> dict:
        with self.lock:
            return {**self.db.summary(), "sources": self.db.source_counts()}

    def recent(self, limit: int = 5) -> list[dict]:
        with self.lock:
            return self.db.recent_documents(limit)

    def verify(self, data: bytes, filename: str | None) -> VerificationResult:
        with self.lock:
            indexed = self.db.summary()["files_indexed"]
            if self._verifier is None or indexed != self._indexed:
                self._verifier = Verifier(self.db, self.store)
                self._indexed = indexed
            return self._verifier.verify(data, filename)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        words = _WORD.findall(query)[:12]
        if not words:
            return []
        fts = " ".join(f'"{w}"' for w in words)
        with self.lock:
            rows = self.db.search(fts, limit * 3, marks=("\x02", "\x03"))
            hits, seen = [], set()
            for r in rows:
                if r["sha256"] in seen:
                    continue
                seen.add(r["sha256"])
                prov = self.db.provenance(r["sha256"])
                if not prov:
                    continue
                p = prov[0]
                hits.append({"title": p.title, "published": p.published, "source": p.source, "url": p.url,
                             "page_no": r["page_no"], "snippet_html": snippet_html(r["snippet"])})
                if len(hits) == limit:
                    break
        return hits

    def thumb(self, sha: str, width: int) -> bytes | None:
        return self._thumb(sha, width)

    def _render_thumb(self, sha: str, width: int) -> bytes | None:
        with self.lock:
            row = self.db.file(sha)
            if row is None or row["kind"] != "pdf" or not self.store.has(sha):
                return None
            try:
                img = render_pdf_page(self.store.get(sha), 1, width, grayscale=False)
            except Exception:  # noqa: BLE001 - a broken PDF just has no thumbnail
                return None
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=84, optimize=True, progressive=True)
        return buf.getvalue()


def create_app(data_dir: Path) -> FastAPI:
    app = FastAPI(title="circverify", docs_url=None, redoc_url=None, openapi_url=None)
    state = _State(data_dir)
    app.mount("/static", StaticFiles(directory=str(files("circverify") / "static")), name="static")

    async def _read(file: UploadFile) -> bytes | str:
        data = await file.read(MAX_UPLOAD + 1)
        if not data:
            return "The file is empty."
        if len(data) > MAX_UPLOAD:
            return f"That file is larger than {MAX_UPLOAD // 1048576} MB."
        return data

    async def _home(result: VerificationResult | None = None, error: str | None = None, status: int = 200):
        info = await run_in_threadpool(state.info)
        recent = await run_in_threadpool(state.recent) if result is None and info["files_indexed"] else []
        html = render_page(info, result, error=error, max_bytes=MAX_UPLOAD, sources=info["sources"],
                           recent=recent, thumb_url=thumb_url)
        return HTMLResponse(html, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return await _home()

    @app.post("/verify", response_class=HTMLResponse)
    async def verify_page(file: UploadFile = File(...), fragment: bool = Query(False)):
        data = await _read(file)
        if isinstance(data, str):
            if fragment:
                return HTMLResponse(f'<p class="error" role="alert">{data}</p>', status_code=400)
            return await _home(error=data, status=400)
        res = await run_in_threadpool(state.verify, data, file.filename)
        if fragment:
            return HTMLResponse(render_result(res, thumb_url=thumb_url))
        return await _home(result=res)

    @app.post("/report")
    async def report(file: UploadFile = File(...)):
        data = await _read(file)
        if isinstance(data, str):
            return JSONResponse({"error": data}, status_code=400)
        res = await run_in_threadpool(state.verify, data, file.filename)
        name = re.sub(r"[^\w.-]+", "-", Path(file.filename or "document").stem)[:60] or "document"
        return Response(render_report(res), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="circverify-report-{name}.html"'})

    @app.get("/search", response_class=HTMLResponse)
    async def search(q: str = Query("", max_length=200)):
        info = await run_in_threadpool(state.info)
        hits = await run_in_threadpool(state.search, q) if q.strip() else []
        return HTMLResponse(render_search(info, q.strip(), hits))

    @app.get("/thumb/{sha}")
    async def thumb(sha: str, w: int = Query(360, ge=120, le=1200)):
        if not _SHA.match(sha):
            return Response(status_code=404)
        png = await run_in_threadpool(state.thumb, sha, w)
        if png is None:
            return Response(status_code=404)
        return Response(png, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/verify")
    async def verify_api(file: UploadFile = File(...)):
        data = await _read(file)
        if isinstance(data, str):
            return JSONResponse({"error": data}, status_code=400)
        res = await run_in_threadpool(state.verify, data, file.filename)
        return res.to_dict()

    @app.get("/api/stats")
    async def stats():
        info = await run_in_threadpool(state.info)
        info.pop("sources", None)
        return info

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return Response(favicon_svg(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico():
        return Response(status_code=204)

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
            info = await run_in_threadpool(state.info)
            return HTMLResponse(render_not_found(info), status_code=404)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
