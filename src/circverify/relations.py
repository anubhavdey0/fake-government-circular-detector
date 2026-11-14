"""Which later documents correct, amend, extend, replace or withdraw an earlier one.

A genuine circular can still mislead: an old deadline forwarded after it was extended, a notice that
a corrigendum changed, a letter later withdrawn. This module links documents so a verified result
can say "a later document may update this one" and point to it.

Two kinds of evidence, both kept with the link so the report can quote them:

* **Citations.** A later document quotes an earlier one's reference number (``No. F.1-15/2026``)
  next to words such as "in supersession of", "corrigendum", "partial modification", "extension of
  the last date" or "withdrawn".
* **Titles.** A later "Corrigendum ...", "Extension of last date ...", "Revised ..." notice whose
  title closely matches an earlier notice's title from the previous eighteen months.

These are heuristics, presented as "may update", never as a verdict.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .db import Database
from .fields import _REF, ref_key

# Most decisive first: a window that mentions withdrawal and amendment is a withdrawal.
KINDS: list[tuple[str, re.Pattern]] = [
    ("withdraws", re.compile(r"\b(withdrawn|withdraw[sn]?|withdrawal|cancell?ed|cancellation|rescind(?:ed)?)\b", re.I)),
    ("supersedes", re.compile(r"\b(supersession|supersed(?:e|es|ed|ing))\b", re.I)),
    ("corrects", re.compile(r"\b(corrigend(?:um|a)|errat(?:um|a))\b", re.I)),
    ("amends", re.compile(r"\b(addend(?:um|a)|amendments?|amended|partial(?:ly)?\s+modif\w*|modification|"
                          r"revised|revision)\b", re.I)),
    ("extends", re.compile(r"\b(?:extension\s+(?:of|in)\s+(?:the\s+)?(?:last|due|closing|submission)\s+date|"
                           r"(?:last|due|closing)\s+date\s+(?:\w+\s+){0,4}(?:has\s+been\s+|is\s+)?extended|"
                           r"date\s+extended)\b", re.I)),
]
FOLLOWS = re.compile(r"\b(in\s+continuation\s+of|further\s+to|with\s+reference\s+to)\b", re.I)
UPDATING = ("withdraws", "supersedes", "corrects", "amends", "extends")
LABELS = {"withdraws": "withdraws it", "supersedes": "replaces it", "corrects": "corrects it",
          "amends": "amends it", "extends": "extends its dates", "follows": "follows up on it"}
_STOP = {"ugc", "the", "of", "for", "and", "to", "in", "on", "a", "an", "regarding", "reg", "letter", "notice",
         "public", "by", "from", "with", "under", "at", "is", "be", "all", "regd", "re", "about"}
_TITLE_KIND = [(k, p) for k, p in KINDS] + [("extends", re.compile(r"\bextension\b.*\bdate\b|\bdate\b.*\bextension\b", re.I))]
WINDOW = 260
MAX_GAP_DAYS = 540
TITLE_MATCH = 0.55  # share of distinctive title words in common
MIN_SHARED_WORDS = 3
# A document whose citations would "update" more than this many earlier documents is a listing (a
# feedback form naming regulations, a reminder quoting earlier letters): it refers to them, it does
# not amend them all. Such citation links are kept as "follows".
MAX_UPDATED_BY_CITATION = 3


def _day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None


def _words(title: str) -> set[str]:
    words = set(re.findall(r"[a-z][a-z0-9]{2,}", title.lower())) - _STOP
    for _, pat in KINDS:
        words = {w for w in words if not pat.fullmatch(w)}
    return words - {"corrigendum", "addendum", "extension", "revised", "date", "last", "withdrawal", "amendment"}


def classify(window: str) -> str | None:
    for kind, pat in KINDS:
        if pat.search(window):
            return kind
    return "follows" if FOLLOWS.search(window) else None


def build(db: Database) -> dict[str, int]:
    """Rebuild the relations table from the index. Returns counts per kind."""
    docs = db.relation_inputs()
    by_ref: dict[str, list[str]] = defaultdict(list)
    info: dict[str, dict] = {}
    for d in docs:
        info[d["sha256"]] = d
        if d["ref_key"]:
            by_ref[d["ref_key"]].append(d["sha256"])
    found: dict[tuple[str, str], tuple[str, str]] = {}

    def add(later: str, earlier: str, kind: str, evidence: str) -> None:
        key = (later, earlier)
        rank = {k: i for i, k in enumerate((*UPDATING, "follows"))}
        if key not in found or rank[kind] < rank[found[key][0]]:
            found[key] = (kind, evidence[:300])

    # Citations of another document's reference number.
    cited: dict[str, set[str]] = defaultdict(set)
    for sha, d in info.items():
        when = _day(d["published"])
        if when is None:
            continue
        text = db.first_pages_text(sha, 3)
        own = d["ref_key"]
        for m in _REF.finditer(text):
            key = ref_key(m.group(1))
            if not key or key == own or key not in by_ref:
                continue
            window = text[max(0, m.start() - WINDOW): m.end() + WINDOW]
            kind = classify(window) or classify(d["subject"] or "") or None
            if kind is None:
                continue
            for other in by_ref[key]:
                o_when = _day(info[other]["published"])
                if other != sha and o_when is not None and o_when < when:
                    add(sha, other, kind, " ".join(window.split()))
                    cited[sha].add(other)
    for later, earlier in cited.items():
        updated = [e for e in earlier if found[(later, e)][0] in UPDATING]
        if len(updated) > MAX_UPDATED_BY_CITATION:
            for e in updated:
                found[(later, e)] = ("follows", found[(later, e)][1])

    # Titles: "Corrigendum ...", "Extension of last date ...", "Revised ..." matching an earlier title.
    dated = sorted(((s, _day(d["published"])) for s, d in info.items() if _day(d["published"])), key=lambda t: t[1])
    title_words = {s: _words(info[s]["title"] or "") for s, _ in dated}
    for i, (sha, when) in enumerate(dated):
        title = info[sha]["title"] or ""
        kind = next((k for k, p in _TITLE_KIND if p.search(title)), None)
        if kind is None or len(title_words[sha]) < 3:
            continue
        best, best_score = None, 0.0
        for other, o_when in reversed(dated[:i]):
            if (when - o_when).days > MAX_GAP_DAYS:
                break
            if o_when == when or other == sha:
                continue
            a, b = title_words[sha], title_words[other]
            shared = len(a & b)
            score = shared / len(a | b) if a and b else 0.0
            if shared >= MIN_SHARED_WORDS and score > best_score:
                best, best_score = other, score
        if best is not None and best_score >= TITLE_MATCH:
            add(sha, best, kind, f"title: {title}")

    db.save_relations([(later, earlier, kind, ev) for (later, earlier), (kind, ev) in found.items()])
    counts: dict[str, int] = defaultdict(int)
    for kind, _ in found.values():
        counts[kind] += 1
    return dict(counts)
