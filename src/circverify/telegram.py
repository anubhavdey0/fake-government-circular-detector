"""A Telegram bot: forward it a circular and it replies with the verdict.

Uses the Bot HTTP API directly (long polling), so it needs no extra library
and no public server. Create a bot with @BotFather, then run
``circverify telegram --token <token>``. With ``--public-url`` pointing at a
public circverify web app, replies include a receipt link people can post
back into the group.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .chat import HELP, LABELS, RecentSubmissions, file_report, format_reply, report_request  # noqa: F401
from .db import Database
from .matcher import UNREADABLE, Verifier
from .store import BlobStore

API = "https://api.telegram.org"
MAX_FILE = 20 * 1024 * 1024  # the Bot API will not hand bots larger files


class TelegramBot:
    def __init__(self, token: str, data_dir: Path, *, public_url: str | None = None,
                 transport: httpx.BaseTransport | None = None, api: str = API):
        self.token = token
        self.api = api.rstrip("/")
        self.public_url = public_url.rstrip("/") if public_url else None
        self.client = httpx.Client(timeout=httpx.Timeout(70.0, connect=20.0), transport=transport)
        self.db = Database(Path(data_dir) / "index.sqlite3")
        self.store = BlobStore(Path(data_dir))
        self._verifier: Verifier | None = None
        self._indexed = -1
        self.recent = RecentSubmissions()

    def verifier(self) -> Verifier:
        indexed = self.db.summary()["files_indexed"]
        if self._verifier is None or indexed != self._indexed:
            self._verifier = Verifier(self.db, self.store)
            self._indexed = indexed
        return self._verifier

    def call(self, method: str, **params) -> dict:
        resp = self.client.post(f"{self.api}/bot{self.token}/{method}", json=params)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description") or f"{method} failed")
        return body["result"]

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > MAX_FILE:
            raise ValueError("file too large")
        resp = self.client.get(f"{self.api}/file/bot{self.token}/{info['file_path']}")
        resp.raise_for_status()
        return resp.content

    def reply(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        self.call("sendMessage", **params)

    def handle(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id, mid = msg["chat"]["id"], msg.get("message_id")
        text = (msg.get("text") or "").strip()
        if text.startswith(("/start", "/help")):
            self.reply(chat_id, HELP)
            return
        wants_report, seen_where = report_request(text) if text and not msg.get("photo") else (False, None)
        if wants_report:
            self.reply(chat_id, file_report(self.db, self.store, self.verifier(), self.recent.take(str(chat_id)),
                                            channel="telegram", seen_where=seen_where), mid)
            return
        data = None
        try:
            if msg.get("photo"):
                data, name = self.download(msg["photo"][-1]["file_id"]), "photo.jpg"
                res = self.verifier().verify(data, name)
            elif msg.get("document"):
                doc = msg["document"]
                data, name = self.download(doc["file_id"]), doc.get("file_name") or "document"
                res = self.verifier().verify(data, name)
            elif text:
                name = "Forwarded text"
                res = self.verifier().verify_text(text, name)
            else:
                self.reply(chat_id, "Send a photo, a PDF, or the text of the circular.", mid)
                return
        except ValueError:
            self.reply(chat_id, "That file is larger than Telegram lets bots download (20 MB).", mid)
            return
        self.recent.remember(str(chat_id), data=data, text=None if data is not None else text, filename=name,
                             verdict=res.verdict)
        receipt_url = None
        if self.public_url and res.verdict != UNREADABLE:
            from .receipts import create_receipt

            receipt_url = f"{self.public_url}/r/{create_receipt(self.db, res)}"
        self.reply(chat_id, format_reply(res, receipt_url), mid)

    def run_forever(self, poll_timeout: int = 50) -> None:
        offset = None
        while True:
            try:
                params = {"timeout": poll_timeout}
                if offset is not None:
                    params["offset"] = offset
                for update in self.call("getUpdates", **params):
                    offset = update["update_id"] + 1
                    try:
                        self.handle(update)
                    except Exception:  # noqa: BLE001 - one bad message must not stop the bot
                        pass
            except (httpx.HTTPError, RuntimeError):
                time.sleep(5)
