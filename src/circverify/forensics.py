"""File-level signals from a submitted PDF.

These are context, not a verdict: genuine files are re-saved and printed all
the time. They become useful next to the official copy ("the official file
was produced by a scanner; this one by an online PDF editor and saved twice
since").
"""

from __future__ import annotations

import re
from collections import Counter

_ANNOT = re.compile(rb"/Subtype\s*/(FreeText|Stamp|Square|Ink|Text|Highlight|Redact)\b")
_EDITORS = re.compile(r"ilovepdf|smallpdf|sejda|pdfescape|pdf-?xchange|foxit|nitro|canva|wps|"
                      r"pdfelement|pdffiller|soda ?pdf|camscanner|picsart", re.I)


def _pdf_date(value: str | None) -> str | None:
    if not value:
        return None
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?", value)
    if not m:
        return value
    y, mo, d, h, mi = m.groups()
    out = y
    if mo:
        out += f"-{mo}"
    if d:
        out += f"-{d}"
    if h and mi:
        out += f" {h}:{mi}"
    return out


def pdf_signals(data: bytes, metadata: dict) -> dict:
    return {
        "producer": metadata.get("Producer") or None,
        "creator": metadata.get("Creator") or None,
        "created": _pdf_date(metadata.get("CreationDate")),
        "modified": _pdf_date(metadata.get("ModDate")),
        "incremental_saves": max(0, data.count(b"%%EOF") - 1),
        "annotations": dict(Counter(m.decode() for m in _ANNOT.findall(data))),
    }


def compare_signals(submitted: dict, official: dict | None) -> list[str]:
    notes = []
    tool = " ".join(filter(None, [submitted.get("producer"), submitted.get("creator")]))
    if tool and _EDITORS.search(tool):
        notes.append(f"Submitted PDF was last written by an editing tool ({tool}).")
    if submitted.get("incremental_saves"):
        notes.append(f"Submitted PDF has {submitted['incremental_saves']} incremental save(s) appended after "
                     "it was first written (edits leave this trace).")
    ann = submitted.get("annotations") or {}
    overlay = {k: v for k, v in ann.items() if k in ("FreeText", "Stamp", "Square", "Redact")}
    if overlay:
        notes.append("Submitted PDF contains overlay annotations: "
                     + ", ".join(f"{v} {k}" for k, v in overlay.items()) + ".")
    if official:
        sp, op = submitted.get("producer"), official.get("producer")
        if sp and op and sp != op:
            notes.append(f"Written by different software than the official file (official: {op}; this one: {sp}).")
    return notes
