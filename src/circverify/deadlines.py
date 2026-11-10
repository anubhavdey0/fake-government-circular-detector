"""Deadlines and event dates in an official document, read deterministically from its text.

A forwarded circular is usually acted on because of a date in it: a last date for applications, an
exam schedule, a window for comments. Showing those dates from the *official* text, and whether
they have passed, answers the practical question even for a genuine document ("this is real, but
its last date was 15 November and has passed"). No model is involved: dates are found with the same
patterns as the reference-number reader, and a date counts as a deadline only when words like
"last date", "on or before" or "not later than" stand right before it.
"""

from __future__ import annotations

import re
from datetime import date

from .config import ISSUER_TZ
from .fields import find_dates

# Anywhere earlier in the same sentence: "extend the last date for applications ... to 15 November".
_DEADLINE_SENTENCE = re.compile(r"last\s+date|deadline|on\s+or\s+before|not\s+later\s+than|latest\s+by|"
                                r"due\s+date|closing\s+date|extended\s+(?:up\s*)?to", re.I)
# Right before the date only: "by 30 June", "till 5 May".
_DEADLINE_NEAR = re.compile(r"\b(till|until|up\s*to|upto|before|by)\W{0,3}$", re.I)
_EVENT = re.compile(r"\b(held|conducted|scheduled|commenc\w+|begin\w*|start\w*|observ\w+|celebrat\w+|"
                    r"w\.?e\.?f\.?|with\s+effect\s+from|effective\s+from|from)\b", re.I)
CONTEXT = 160


def key_dates(text: str, issued: str | None = None, limit: int = 6) -> list[dict]:
    """``[{date, kind, context}]`` for deadlines and events, in date order; the issue date itself is skipped."""
    out: dict[str, dict] = {}
    text = " ".join((text or "").split())  # PDF text breaks lines mid-sentence
    for pos, iso in find_dates(text):
        if iso == issued:
            continue
        before = text[max(0, pos - CONTEXT): pos]
        cut = max(before.rfind(". "), before.rfind("; "))
        sentence = before[cut + 1:] if cut >= 0 else before
        if _DEADLINE_SENTENCE.search(sentence) or _DEADLINE_NEAR.search(sentence):
            kind = "deadline"
        elif _EVENT.search(sentence[-60:]):
            kind = "event"
        else:
            continue
        tail = text[pos: pos + 60]
        stop = max(tail.rfind(" ", 0, 48), tail.find(". "))
        tail = tail[:stop] if stop > 6 else tail[:24]
        snippet = " ".join((sentence + tail).split())
        prev = out.get(iso)
        if prev is None or (kind == "deadline" and prev["kind"] != "deadline"):
            if len(snippet) > 150:  # keep the end of the sentence, starting on a whole word
                snippet = snippet[-150:]
                snippet = "…" + snippet[snippet.find(" ") + 1:]
            out[iso] = {"date": iso, "kind": kind, "context": snippet}
    return sorted(out.values(), key=lambda d: d["date"])[:limit]


def today() -> date:
    from datetime import datetime

    return datetime.now(ISSUER_TZ).date()


def describe_when(iso: str, ref: date | None = None) -> str:
    d, ref = date.fromisoformat(iso), ref or today()
    days = (d - ref).days
    if days < 0:
        return "passed"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days" if days <= 60 else "upcoming"
