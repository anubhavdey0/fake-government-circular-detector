"""Structured fields read from a circular: reference number, date, subject and signatory.

Indian government letters follow a consistent shape: a file reference such as
``F.No. 1-15/2026 (CPP-II)`` or ``D.O.No.30-57/2012(CVO)`` near the top, the
letter date beside it, a ``Subject:`` line, and the signatory's name in
brackets above their designation at the bottom. These fields give a second,
independent route to the official document ("this reference number belongs
to notice X") and make differences easy to state ("dated 15 December in the
submission, 13 December in the original").

Many text layers on UGC's site were produced by scanner OCR, so the patterns
tolerate its usual slips: ``0`` for ``O`` in "D.O.No.", a missing dot after
"F", and a closing bracket read as ``I``, ``l`` or ``|``.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

_REF = re.compile(
    r"(?:\bD\.?\s*[O0]\.?\s*No\.?|\bF\.?\s*No\.?|\bNo\.?\s*F\.?|\bF\.(?=\s*\d)|फा\.?\s*सं\.?)\s*[:.\-]?\s*"
    r"(\d{1,4}\s*[-–]\s*\d{1,5}\s*/\s*\d{2,4}(?:\s*[-–]\s*\d{1,4})?(?:\s*\(\s*[^)\n]{1,40}\))?)",
    re.I,
)
_D_MONTH_Y = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?" + _MONTH + r"[\s,.]*(\d{4})\b", re.I)
_MONTH_D_Y = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\b", re.I)
_NUMERIC = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
_SUBJECT = re.compile(r"(?im)^\s*(?:subject|sub)\s*[:.\-]+\s*(.{8,})$")
# A bracketed name of two to five capitalised words, title optional.
_SIGNATORY = re.compile(
    r"\(\s*((?:(?:Prof|Dr|Shri|Smt|Sh|Mr|Ms|Mrs|Kum)\.?\s*)?[A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]*){1,4}?)\s*[)Il|]")
_DESIGNATION = re.compile(
    r"\b((?:Joint|Deputy|Under|Additional|Assistant|Principal)?\s*Secretary|Chairman|Chairperson|Vice[- ]Chairman|"
    r"Education Officer|Section Officer|Director|Advisor|Adviser|Registrar|Controller of Examinations)\b", re.I)


def ref_key(ref: str | None) -> str | None:
    """A comparable form of a reference number: letters and digits only, upper case."""
    if not ref:
        return None
    key = re.sub(r"[^0-9A-Z]", "", ref.upper())
    return key if len(key) >= 5 else None


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        parsed = date(y, m, d)
    except ValueError:
        return None
    return parsed.isoformat() if 1950 <= parsed.year <= 2100 else None


def find_dates(text: str) -> list[tuple[int, str]]:
    """Every date in ``text`` as ``(position, iso)``, in reading order."""
    found = []
    for m in _D_MONTH_Y.finditer(text):
        iso = _iso(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        if iso:
            found.append((m.start(), iso))
    for m in _MONTH_D_Y.finditer(text):
        iso = _iso(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        if iso:
            found.append((m.start(), iso))
    for m in _NUMERIC.finditer(text):
        iso = _iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))  # Indian order: day first
        if iso:
            found.append((m.start(), iso))
    return sorted(found)


def extract_fields(text: str) -> dict:
    """Best-effort fields from the text of a circular's first page or two."""
    text = text or ""
    head, tail = text[:2500], text[-2000:]
    out: dict = {"ref_no": None, "ref_key": None, "doc_date": None, "subject": None,
                 "signatory": None, "designation": None}
    m = _REF.search(head)
    if m:
        ref = re.sub(r"\s+", " ", m.group(1)).strip()
        out["ref_no"] = ref
        out["ref_key"] = ref_key(ref)
    dates = find_dates(head)
    if dates:
        out["doc_date"] = dates[0][1]
    m = _SUBJECT.search(head)
    if m:
        out["subject"] = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    sigs = list(_SIGNATORY.finditer(tail))
    if sigs:
        # Prefer the last bracketed name that is followed by a designation.
        chosen, designation = sigs[-1], None
        for sig in reversed(sigs):
            d = _DESIGNATION.search(tail[sig.end(): sig.end() + 120])
            if d:
                chosen, designation = sig, d
                break
        out["signatory"] = re.sub(r"\s+", " ", chosen.group(1)).strip()
        if designation:
            out["designation"] = re.sub(r"\s+", " ", designation.group(1)).strip().title()
    return out
