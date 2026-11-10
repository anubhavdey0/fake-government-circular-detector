"""A WhatsApp bot on Meta's WhatsApp Cloud API: forward it a circular, get the verdict back.

Fake circulars spread in WhatsApp family and college groups, so this is where a check is most useful.
The bot is a webhook served by ``circverify serve``; it is switched on when these are set:

* ``CIRCVERIFY_WHATSAPP_TOKEN``: a (system user) access token for the WhatsApp Business account;
* ``CIRCVERIFY_WHATSAPP_PHONE_ID``: the phone number ID that sends the replies;
* ``CIRCVERIFY_WHATSAPP_VERIFY_TOKEN``: any secret string, entered again in the Meta app's webhook settings;
* ``CIRCVERIFY_WHATSAPP_APP_SECRET``: the Meta app secret, used to check that each delivery really comes
  from Meta (``X-Hub-Signature-256``). Deliveries without a valid signature are refused.
* ``CIRCVERIFY_WHATSAPP_API_VERSION`` (optional): Graph API version, default ``v23.0``.

The webhook URL is ``https://<your public address>/whatsapp/webhook``; Meta requires HTTPS, so a local
server needs a tunnel (cloudflared, ngrok). Subscribe the app to the ``messages`` field.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from .chat import HELP, RecentSubmissions, file_report, format_reply, report_request

GRAPH = "https://graph.facebook.com"
MAX_MEDIA = 30 * 1024 * 1024


@dataclass
class WhatsAppConfig:
    token: str
    phone_id: str
    verify_token: str
    app_secret: str
    api_version: str = "v23.0"
    public_url: str | None = None

    @classmethod
    def from_env(cls, public_url: str | None = None) -> WhatsAppConfig | None:
        env = os.environ
        keys = ("CIRCVERIFY_WHATSAPP_TOKEN", "CIRCVERIFY_WHATSAPP_PHONE_ID", "CIRCVERIFY_WHATSAPP_VERIFY_TOKEN",
                "CIRCVERIFY_WHATSAPP_APP_SECRET")
        if not all(env.get(k) for k in keys):
            return None
        return cls(env[keys[0]], env[keys[1]], env[keys[2]], env[keys[3]],
                   env.get("CIRCVERIFY_WHATSAPP_API_VERSION") or "v23.0", public_url)


class WhatsAppBot:
    """Handles webhook deliveries. ``state`` is the web app's state: it owns the verifier and its lock."""

    def __init__(self, config: WhatsAppConfig, state, *, transport: httpx.BaseTransport | None = None,
                 api: str = GRAPH):
        self.config = config
        self.state = state
        self.base = f"{api.rstrip('/')}/{config.api_version}"
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), transport=transport,
                                   headers={"Authorization": f"Bearer {config.token}"})
        self.recent = RecentSubmissions()
        self._seen: OrderedDict[str, None] = OrderedDict()  # Meta may deliver a message more than once

    # -- webhook plumbing -------------------------------------------------------

    def verify_subscription(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        """The GET handshake Meta does when the webhook URL is saved."""
        if mode == "subscribe" and token and hmac.compare_digest(token, self.config.verify_token):
            return challenge or ""
        return None

    def signature_ok(self, body: bytes, header: str | None) -> bool:
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self.config.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.removeprefix("sha256="))

    def messages(self, payload: dict) -> list[dict]:
        out = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    mid = msg.get("id")
                    if mid and mid in self._seen:
                        continue
                    if mid:
                        self._seen[mid] = None
                        while len(self._seen) > 2000:
                            self._seen.popitem(last=False)
                    out.append(msg)
        return out

    # -- Graph API ----------------------------------------------------------------

    def download(self, media_id: str) -> bytes:
        meta = self.client.get(f"{self.base}/{media_id}")
        meta.raise_for_status()
        info = meta.json()
        if int(info.get("file_size") or 0) > MAX_MEDIA:
            raise ValueError("file too large")
        resp = self.client.get(info["url"])
        resp.raise_for_status()
        return resp.content

    def reply(self, to: str, text: str, reply_to: str | None = None) -> None:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                "text": {"preview_url": False, "body": text}}
        if reply_to:
            body["context"] = {"message_id": reply_to}
        self.client.post(f"{self.base}/{self.config.phone_id}/messages", json=body).raise_for_status()

    # -- one message ----------------------------------------------------------------

    def handle(self, msg: dict) -> None:
        sender, mid, kind = msg.get("from"), msg.get("id"), msg.get("type")
        if not sender:
            return
        text = ((msg.get("text") or {}).get("body") or "").strip()
        if kind == "text":
            if text.lower() in ("hi", "hello", "help", "/help", "/start", "start"):
                self.reply(sender, HELP, mid)
                return
            wants_report, seen_where = report_request(text)
            if wants_report:
                with self.state.lock:
                    reply = file_report(self.state.db, self.state.store, self.state._current_verifier(),
                                        self.recent.take(sender), channel="whatsapp", seen_where=seen_where)
                self.reply(sender, reply, mid)
                return
        data = None
        try:
            if kind == "image":
                data, name = self.download(msg["image"]["id"]), "photo.jpg"
            elif kind == "document":
                doc = msg["document"]
                data, name = self.download(doc["id"]), doc.get("filename") or "document"
            elif kind == "text" and text:
                name = "Forwarded text"
            else:
                self.reply(sender, "Send a photo, a PDF, or the text of the circular.", mid)
                return
        except ValueError:
            self.reply(sender, "That file is too large to check (30 MB at most).", mid)
            return
        res, _ = self.state.verify(data, name) if data is not None else self.state.verify_text(text)
        self.recent.remember(sender, data=data, text=None if data is not None else text, filename=name,
                             verdict=res.verdict)
        receipt_url = None
        if self.config.public_url and res.verdict != "UNREADABLE":
            from .receipts import create_receipt

            with self.state.lock:
                receipt_url = f"{self.config.public_url.rstrip('/')}/r/{create_receipt(self.state.db, res)}"
        self.reply(sender, format_reply(res, receipt_url), mid)

    def handle_payload(self, payload: dict) -> None:
        for msg in self.messages(payload):
            try:
                self.handle(msg)
            except Exception:  # noqa: BLE001 - one bad message must not stop the others
                pass
