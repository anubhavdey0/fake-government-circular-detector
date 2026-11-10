"""The local web app: ``circverify serve``.

Routes: ``/`` check page, ``/verify`` and ``/verify-text`` (form posts;
``?fragment=1`` for the in-page result), ``/report`` (same check, downloadable
HTML), ``/receipt`` and ``/r/<id>`` (shareable receipts), ``/search``,
``/coverage``, ``/feed.xml``, ``/v/<sha256>`` (fingerprint check, the target
of a printed QR code), ``/thumb/<sha256>``, ``/api/verify`` and ``/api/stats``.
"""

from __future__ import annotations

import io
from html import escape
import json
import re
import secrets
import threading
from collections import OrderedDict
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from PIL import Image
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import Database
from .extract import render_pdf_page
from .fields import ref_key
from .htmlreport import (
    atom_feed,
    render_bulk,
    render_coverage,
    render_hash_page,
    render_not_found,
    render_page,
    render_receipt,
    render_report,
    render_result,
    SOURCE_NAMES,
    render_search,
    snippet_html,
)
from .crowd import create_report
from .fetchurl import LinkError, verify_link
from .receipts import create_receipt, load_receipt
from .icons import favicon_svg
from .matcher import VerificationResult, Verifier
from .store import BlobStore
from .whatsapp import WhatsAppBot, WhatsAppConfig

MAX_UPLOAD = 30 * 1024 * 1024
MAX_BULK = 20
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_WORD = re.compile(r"[\wऀ-ॣ०-෿]+")


_LOOKS_LIKE_REF = re.compile(
    r"(\d{1,4}\s*[-\u2013]\s*\d{1,5}\s*/\s*\d{2,4}(?:\s*[-\u2013]\s*\d{1,4})?(?:\s*\(\s*[^)\n]{1,40}\))?)")


def _keep(p, source: str | None, date_from: str | None, date_to: str | None, status: str) -> bool:
    if source and p.source != source:
        return False
    if date_from and (not p.published or p.published < date_from):
        return False
    if date_to and (not p.published or p.published > date_to):
        return False
    if status == "listed" and (p.source == "archive" or p.removed_on):
        return False
    if status == "withdrawn" and not p.removed_on:
        return False
    if status == "archived" and p.source != "archive":
        return False
    return True


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
        self._fakes = -1
        # Recent results, so a receipt can be made without uploading the file again.
        self._results: OrderedDict[str, VerificationResult] = OrderedDict()
        self._thumb = lru_cache(maxsize=256)(self._render_thumb)

    def info(self) -> dict:
        with self.lock:
            return {**self.db.summary(), "sources": self.db.source_counts()}

    def recent(self, limit: int = 8) -> list[dict]:
        with self.lock:
            return self.db.recent_documents(limit)

    def _current_verifier(self) -> Verifier:
        indexed = self.db.summary()["files_indexed"]
        fakes = self.db.conn.execute("SELECT COUNT(*) FROM fake_pages").fetchone()[0]
        if self._verifier is None or indexed != self._indexed or fakes != self._fakes:
            self._verifier = Verifier(self.db, self.store)
            self._indexed, self._fakes = indexed, fakes
        return self._verifier

    def _remember(self, res: VerificationResult) -> str:
        token = secrets.token_urlsafe(12)
        self._results[token] = res
        while len(self._results) > 128:
            self._results.popitem(last=False)
        return token

    def verify(self, data: bytes, filename: str | None) -> tuple[VerificationResult, str]:
        with self.lock:
            res = self._current_verifier().verify(data, filename)
            return res, self._remember(res)

    def verify_link(self, url: str) -> tuple[VerificationResult, str]:
        with self.lock:
            res = verify_link(self._current_verifier(), url)
            return res, self._remember(res)

    def verify_text(self, text: str) -> tuple[VerificationResult, str]:
        with self.lock:
            res = self._current_verifier().verify_text(text)
            return res, self._remember(res)

    def report_fake(self, **kw) -> tuple[str | None, str]:
        with self.lock:
            return create_report(self.db, self.store, self._current_verifier(), channel="web", **kw)

    def receipt_for(self, token: str) -> str | None:
        with self.lock:
            res = self._results.get(token)
            return create_receipt(self.db, res) if res is not None else None

    def receipt(self, rid: str):
        with self.lock:
            return load_receipt(self.db, rid)

    def coverage(self) -> tuple[dict, list]:
        with self.lock:
            return self.db.coverage(), [dict(r) for r in self.db.known_fakes()]

    def newest(self) -> list[dict]:
        with self.lock:
            return self.db.newest_notices(50)

    def provenance(self, sha: str):
        with self.lock:
            return self.db.provenance(sha)

    def browse(self, **filters) -> tuple[list[dict], int]:
        with self.lock:
            return self.db.browse(**filters)

    def search(self, query: str, limit: int = 20, *, source: str | None = None, date_from: str | None = None,
               date_to: str | None = None, status: str = "all") -> list[dict]:
        # A reference number ("F.No. 1-15/2026", "1-15/2026") goes straight to the documents that carry it.
        m = _LOOKS_LIKE_REF.search(query)
        key = ref_key(m.group(1)) if m else None
        if key:
            rows, _ = self.browse(ref_key=key, source=source, date_from=date_from, date_to=date_to,
                                  status=status, limit=limit)
            return [{"title": r["title"], "published": r["published"], "source": r["source"], "url": r["url"],
                     "page_no": None, "snippet_html": "", "removed_on": r["removed_on"], "by_reference": True}
                    for r in rows]
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
                prov = [p for p in self.db.provenance(r["sha256"]) if _keep(p, source, date_from, date_to, status)]
                if not prov:
                    continue
                p = prov[0]
                hits.append({"title": p.title, "published": p.published, "source": p.source, "url": p.url,
                             "page_no": r["page_no"], "snippet_html": snippet_html(r["snippet"]),
                             "removed_on": p.removed_on})
                if len(hits) == limit:
                    break
        return hits

    def thumb(self, sha: str, width: int) -> bytes | None:
        return self._thumb(sha, width)

    def _render_thumb(self, sha: str, width: int) -> bytes | None:
        with self.lock:
            if not self.store.has(sha):
                return None
            row = self.db.file(sha)
            try:
                if row is not None and row["kind"] == "pdf":
                    img = render_pdf_page(self.store.get(sha), 1, width, grayscale=False)
                elif self.db.conn.execute("SELECT 1 FROM fakes WHERE sha256 = ?", (sha,)).fetchone():
                    # A debunked fake: the image published with the debunk (shown as evidence).
                    img = Image.open(io.BytesIO(self.store.get(sha)))
                    img.thumbnail((width, width * 3))
                else:
                    return None
            except Exception:  # noqa: BLE001 - a broken file just has no thumbnail
                return None
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=84, optimize=True, progressive=True)
        return buf.getvalue()


def create_app(data_dir: Path, *, whatsapp: WhatsAppConfig | None = None,
               whatsapp_transport=None) -> FastAPI:
    app = FastAPI(title="circverify", docs_url=None, redoc_url=None, openapi_url=None)
    state = _State(data_dir)
    app.mount("/static", StaticFiles(directory=str(files("circverify") / "static")), name="static")
    app.state.whatsapp = None
    if whatsapp is not None:
        bot = WhatsAppBot(whatsapp, state, transport=whatsapp_transport)
        app.state.whatsapp = bot

        @app.get("/whatsapp/webhook", include_in_schema=False)
        async def whatsapp_verify(request: Request):
            q = request.query_params
            challenge = bot.verify_subscription(q.get("hub.mode"), q.get("hub.verify_token"), q.get("hub.challenge"))
            if challenge is None:
                return Response(status_code=403)
            return Response(challenge, media_type="text/plain")

        @app.post("/whatsapp/webhook", include_in_schema=False)
        async def whatsapp_receive(request: Request, background: BackgroundTasks):
            body = await request.body()
            if not bot.signature_ok(body, request.headers.get("x-hub-signature-256")):
                return Response(status_code=401)
            try:
                payload = json.loads(body)
            except ValueError:
                return Response(status_code=400)
            # Meta expects a quick 200; the check and the reply happen afterwards.
            background.add_task(run_in_threadpool, bot.handle_payload, payload)
            return Response(status_code=200)

    async def _read(file: UploadFile) -> bytes | str:
        data = await file.read(MAX_UPLOAD + 1)
        if not data:
            return "The file is empty."
        if len(data) > MAX_UPLOAD:
            return f"That file is larger than {MAX_UPLOAD // 1048576} MB."
        return data

    async def _home(result: VerificationResult | None = None, error: str | None = None, status: int = 200,
                    token: str | None = None):
        info = await run_in_threadpool(state.info)
        recent = await run_in_threadpool(state.recent) if result is None and info["files_indexed"] else []
        html = render_page(info, result, error=error, max_bytes=MAX_UPLOAD, sources=info["sources"],
                           recent=recent, thumb_url=thumb_url, token=token)
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
        res, token = await run_in_threadpool(state.verify, data, file.filename)
        if fragment:
            return HTMLResponse(render_result(res, thumb_url=thumb_url, token=token))
        return await _home(result=res, token=token)

    @app.post("/verify-url", response_class=HTMLResponse)
    async def verify_url(url: str = Form(..., max_length=2000), fragment: bool = Query(False)):
        try:
            res, token = await run_in_threadpool(state.verify_link, url)
        except LinkError as exc:
            if fragment:
                return HTMLResponse(f'<p class="error" role="alert">{escape(str(exc))}</p>', status_code=400)
            return await _home(error=str(exc), status=400)
        if fragment:
            return HTMLResponse(render_result(res, thumb_url=thumb_url, token=token))
        return await _home(result=res, token=token)

    @app.post("/verify-bulk", response_class=HTMLResponse)
    async def verify_bulk(files: list[UploadFile] = File(...)):
        """Several files at once: one row each, with the official document or debunk."""
        rows = []
        for f in files[:MAX_BULK]:
            data = await _read(f)
            if isinstance(data, str):
                rows.append((f.filename or "file", None, data))
                continue
            res, _ = await run_in_threadpool(state.verify, data, f.filename)
            rows.append((f.filename or "file", res, None))
        return HTMLResponse(render_bulk(rows, skipped=max(0, len(files) - MAX_BULK)))

    @app.post("/verify-text", response_class=HTMLResponse)
    async def verify_text_page(text: str = Form(..., max_length=50_000), fragment: bool = Query(False)):
        res, token = await run_in_threadpool(state.verify_text, text)
        if fragment:
            return HTMLResponse(render_result(res, thumb_url=thumb_url, token=token))
        return await _home(result=res, token=token)

    @app.post("/report-fake")
    async def report_fake(file: UploadFile | None = File(None), text: str = Form(""), seen_where: str = Form(""),
                          note: str = Form(""), debunk_url: str = Form("")):
        data = None
        if file is not None and file.filename:
            data = await _read(file)
            if isinstance(data, str):
                return JSONResponse({"ok": False, "message": data}, status_code=400)
        if data is None and len(text.strip()) < 40:
            return JSONResponse({"ok": False, "message": "Nothing to report."}, status_code=400)
        rid, status = await run_in_threadpool(
            state.report_fake, data=data, text=None if data is not None else text,
            filename=file.filename if data is not None else "pasted text", seen_where=seen_where, note=note,
            debunk_url=debunk_url)
        if rid is None:
            return JSONResponse({"ok": False, "message": status.removeprefix("refused: ").capitalize() + "."})
        msg = ("Thank you. It is now in the review queue. It will be marked as fake only if a public debunk is "
               "found for it." if status == "added" else
               "Thank you. Someone else reported this document too; your report has been added to it.")
        return JSONResponse({"ok": True, "id": rid, "message": msg})

    @app.post("/receipt")
    async def make_receipt(token: str = Form(..., max_length=64)):
        rid = await run_in_threadpool(state.receipt_for, token)
        if rid is None:
            return JSONResponse({"error": "That result has expired; check the document again."}, status_code=404)
        return {"id": rid, "url": f"/r/{rid}"}

    @app.get("/r/{rid}", response_class=HTMLResponse)
    async def show_receipt(rid: str):
        found = await run_in_threadpool(state.receipt, rid) if re.fullmatch(r"[\w-]{6,40}", rid) else None
        info = await run_in_threadpool(state.info)
        if found is None:
            return HTMLResponse(render_not_found(info), status_code=404)
        payload, created = found
        return HTMLResponse(render_receipt(payload, rid, created, info))

    @app.get("/coverage", response_class=HTMLResponse)
    async def coverage_page():
        info = await run_in_threadpool(state.info)
        cov, fakes = await run_in_threadpool(state.coverage)
        return HTMLResponse(render_coverage(info, cov, fakes))

    @app.get("/feed.xml")
    async def feed(request: Request):
        info = await run_in_threadpool(state.info)
        notices = await run_in_threadpool(state.newest)
        base = str(request.base_url).rstrip("/")
        return Response(atom_feed(notices, base, info.get("last_listing_sync")), media_type="application/atom+xml")

    @app.get("/v/{sha}", response_class=HTMLResponse)
    async def fingerprint_page(sha: str):
        info = await run_in_threadpool(state.info)
        sha = sha.lower()
        if not _SHA.match(sha):
            return HTMLResponse(render_not_found(info), status_code=404)
        prov = await run_in_threadpool(state.provenance, sha)
        return HTMLResponse(render_hash_page(info, sha, prov))

    @app.post("/report")
    async def report(file: UploadFile = File(...)):
        data = await _read(file)
        if isinstance(data, str):
            return JSONResponse({"error": data}, status_code=400)
        res, _ = await run_in_threadpool(state.verify, data, file.filename)
        name = re.sub(r"[^\w.-]+", "-", Path(file.filename or "document").stem)[:60] or "document"
        return Response(render_report(res), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="circverify-report-{name}.html"'})

    def _filters(source: str, date_from: str, date_to: str, status: str) -> dict:
        return {"source": source if source in SOURCE_NAMES else None,
                "date_from": date_from if _DATE.match(date_from) else None,
                "date_to": date_to if _DATE.match(date_to) else None,
                "status": status if status in ("all", "listed", "withdrawn", "archived") else "all"}

    @app.get("/search", response_class=HTMLResponse)
    async def search(q: str = Query("", max_length=200), source: str = Query("", max_length=40),
                     date_from: str = Query("", alias="from", max_length=10),
                     date_to: str = Query("", alias="to", max_length=10), status: str = Query("all", max_length=12),
                     page: int = Query(1, ge=1, le=1000)):
        info = await run_in_threadpool(state.info)
        f = _filters(source, date_from, date_to, status)
        if q.strip():
            hits = await run_in_threadpool(lambda: state.search(q, **f))
            return HTMLResponse(render_search(info, q.strip(), hits, filters=f))
        # No words: the registry, every document, newest first.
        per = 30
        rows, total = await run_in_threadpool(
            lambda: state.browse(**{**f, "status": f["status"] if f["status"] != "all" else "listed"},
                                 limit=per, offset=(page - 1) * per))
        return HTMLResponse(render_search(info, "", [], filters=f, browse=(rows, total, page, per)))

    @app.get("/api/search")
    async def search_api(q: str = Query("", max_length=200), source: str = Query("", max_length=40),
                         date_from: str = Query("", alias="from", max_length=10),
                         date_to: str = Query("", alias="to", max_length=10), status: str = Query("all", max_length=12),
                         limit: int = Query(20, ge=1, le=100)):
        f = _filters(source, date_from, date_to, status)
        if q.strip():
            hits = await run_in_threadpool(lambda: state.search(q, limit, **f))
            return JSONResponse({"query": q, "results": [{k: v for k, v in h.items() if k != "snippet_html"}
                                                         for h in hits]})
        rows, total = await run_in_threadpool(
            lambda: state.browse(**{**f, "status": f["status"] if f["status"] != "all" else "listed"}, limit=limit))
        return JSONResponse({"query": "", "total": total, "results": rows})

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
        res, _ = await run_in_threadpool(state.verify, data, file.filename)
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
