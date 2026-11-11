"""What the chat bots (Telegram, WhatsApp) share: the reply text, the help text, and a short memory
of each chat's last document so that "report" can file it for review."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from .matcher import ALTERED, KNOWN_FAKE, NO_MATCH, UNCONFIRMED_MATCH, UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT

HELP = (
    "Send me a UGC circular as a photo, screenshot, PDF, or pasted text, and I will check it against every "
    "document UGC has published.\n\n"
    "You will get one of: VERIFIED (UGC published it), ALTERED (a real circular that has been changed), "
    "KNOWN FAKE (publicly debunked), or NOT FOUND (unverified, which is not the same as fake).\n\n"
    "If you have seen a document passed around as genuine, send it and then reply \"report\", optionally with "
    "where you got it (\"report college WhatsApp group\"). Reports are reviewed; nothing is called fake without "
    "a public debunk.\n\n"
    "This is an unofficial tool."
)

LABELS = {
    VERIFIED_EXACT: "VERIFIED: exact copy of an official file",
    VERIFIED_CONTENT: "VERIFIED: same content as an official file",
    UNCONFIRMED_MATCH: "MATCHES, DETAILS UNCONFIRMED: an official page, but a photo cannot rule out a changed date or amount",
    ALTERED: "ALTERED: a real circular, but changed",
    KNOWN_FAKE: "KNOWN FAKE: publicly debunked",
    NO_MATCH: "NOT FOUND: unverified (not the same as fake)",
    UNREADABLE: "COULD NOT READ the file",
}
FOOTER = "\n\nUnofficial check, not affiliated with UGC."
MAX_REPLY = 4000


def format_reply(res, receipt_url: str | None = None) -> str:
    lines = [LABELS.get(res.verdict, res.verdict), "", res.summary]
    m = res.match
    if res.known_fake and res.known_fake.get("debunks"):
        d = res.known_fake["debunks"][0]
        lines += ["", f"Debunk: {d.get('url')}"]
    if m and m.primary:
        lines += ["", f"Official file: {m.primary.url}"]
    if m and m.text and m.text.changes:
        lines += ["", "What changed:"]
        for c in m.text.changes[:6]:
            lines.append(f"- official: {c.official or '(nothing)'} / this copy: {c.submitted or '(removed)'}")
    regions = sum(len(v.boxes) for v in (m.visual if m else []) if v.same_document)
    if regions:
        lines.append(f"- {regions} changed area(s) on the page image")
    to_check = sum(len(v.lines_to_check) for v in (m.visual if m else []) if v.same_document)
    if to_check and not regions:
        lines.append(f"- {to_check} line(s) differ slightly from the official page; compare them by eye")
    if res.withdrawn:
        lines += ["", f"Note: UGC stopped listing this notice on {res.withdrawn}."]
    elif res.archived_only is not None:
        lines += ["", "Note: UGC no longer lists this document; it is known from the Wayback Machine."]
    deadlines = [d for d in res.key_dates if d["kind"] == "deadline"][:2]
    if deadlines and res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT, UNCONFIRMED_MATCH, ALTERED):
        from .deadlines import describe_when

        lines += ["", "Dates in the official document: " + "; ".join(
            f"last date {d['date']} ({describe_when(d['date'])})" for d in deadlines)]
    if res.updated_by:
        d = res.updated_by[0]
        lines += ["", f"Note: a later document may update this one: {d['title']} ({d['published'] or 'undated'}) "
                      f"{d['url']}"]
    hints = [n for n in res.signal_notes if "imitates" in n or "shortened link" in n][:2]
    if hints:
        lines += ["", "Worth knowing:"] + [f"- {h}" for h in hints]
    if res.verdict in (NO_MATCH, ALTERED, UNCONFIRMED_MATCH):
        lines += ["", "Seen this passed around as genuine? Reply \"report\"."]
    if receipt_url:
        lines += ["", f"Receipt: {receipt_url}"]
    return "\n".join(lines)[: MAX_REPLY - len(FOOTER)] + FOOTER


def report_request(text: str) -> tuple[bool, str | None]:
    """``("report college group")`` -> ``(True, "college group")``; anything else -> ``(False, None)``."""
    t = text.strip()
    low = t.lower()
    for cmd in ("/report", "report"):
        if low == cmd or low.startswith(cmd + " "):
            return True, t[len(cmd):].strip() or None
    return False, None


@dataclass
class Submission:
    data: bytes | None
    text: str | None
    filename: str
    verdict: str
    at: float


class RecentSubmissions:
    """The last document each chat sent, kept briefly in memory so "report" can refer to it."""

    def __init__(self, max_chats: int = 200, ttl: float = 30 * 60, max_bytes: int = 10 * 1024 * 1024):
        self.max_chats, self.ttl, self.max_bytes = max_chats, ttl, max_bytes
        self._items: OrderedDict[str, Submission] = OrderedDict()

    def remember(self, chat: str, *, data: bytes | None, text: str | None, filename: str, verdict: str) -> None:
        if data is not None and len(data) > self.max_bytes:
            return
        self._items[chat] = Submission(data, text, filename, verdict, time.monotonic())
        self._items.move_to_end(chat)
        while len(self._items) > self.max_chats:
            self._items.popitem(last=False)

    def take(self, chat: str) -> Submission | None:
        item = self._items.pop(chat, None)
        if item is None or time.monotonic() - item.at > self.ttl:
            return None
        return item


def file_report(db, store, verifier, item: Submission | None, *, channel: str, seen_where: str | None) -> str:
    """Reply text for a "report" message."""
    from .crowd import create_report

    if item is None:
        return "Send the document first (photo, PDF or text), then reply \"report\"."
    if item.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT):
        return "That document is official, so it cannot be reported as fake."
    rid, status = create_report(db, store, verifier, data=item.data, text=item.text, filename=item.filename,
                                channel=channel, seen_where=seen_where)
    if rid is None:
        return status.removeprefix("refused: ").capitalize() + "."
    if status == "counted":
        return "Thank you. Others reported this document too; your report has been added to theirs." + FOOTER
    return ("Thank you, it is in the review queue. It will be marked as fake only if a public debunk is found "
            "for it." + FOOTER)
