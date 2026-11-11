"""The evidence behind a verdict, as a short checklist a reader can scan.

Each row is one independent check with a plain answer: the exact-file match, whether an official
original was found, the reference number and issue date against the official record, changed
dates, numbers and amounts in the text, the page image, the digital signature, links and QR codes,
and whether the official document is still current. There is deliberately no overall score: a
number like 92/100 would suggest a certainty the checks cannot give.
"""

from __future__ import annotations

from .matcher import ALTERED, KNOWN_FAKE, NO_MATCH, UNCONFIRMED_MATCH, UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT

OK, BAD, WARN, NEUTRAL = "ok", "bad", "warn", "neutral"
CRITICAL = ("date", "number", "money", "link", "contact", "meaning")


def _row(check: str, status: str, value: str) -> dict:
    return {"check": check, "status": status, "value": value}


def _critical(change) -> bool:
    return any(any(k in r.lower() for k in CRITICAL) for r in (change.reasons or []))


def summarize(res, db) -> list[dict]:
    if res.verdict == UNREADABLE:
        return []
    rows: list[dict] = []
    m = res.match
    rows.append(_row("Exact file match", OK if res.verdict == VERIFIED_EXACT else NEUTRAL,
                     "Yes, byte for byte" if res.verdict == VERIFIED_EXACT else "No"))
    if res.verdict == KNOWN_FAKE:
        d = (res.known_fake or {}).get("debunks") or [{}]
        rows.append(_row("Public debunk", BAD, f"Matches a document debunked by {d[0].get('source') or 'a fact-checker'}"))
    if m and m.primary:
        rows.append(_row("Official original", OK, f"Found: {m.primary.title}"))
    elif res.verdict in (NO_MATCH, KNOWN_FAKE):
        rows.append(_row("Official original", WARN, "None of the issuer's documents matches"))

    official = db.fields(m.sha256) if m else None
    ref, off_ref = res.fields.get("ref_no"), official["ref_no"] if official else None
    if res.verdict == VERIFIED_EXACT:
        pass
    elif ref and off_ref:
        same = (res.fields.get("ref_key") or ref) == (official["ref_key"] or off_ref)
        rows.append(_row("Reference number", OK if same else BAD,
                         f"Matches the official ({ref})" if same else f"Official {off_ref}; this copy {ref}"))
    elif ref and res.verdict == NO_MATCH:
        owners = db.files_with_ref(res.fields.get("ref_key") or "")
        rows.append(_row("Reference number", WARN,
                         f"{ref} belongs to a different official document" if owners else f"{ref} is not in the index"))
    elif m:
        rows.append(_row("Reference number", NEUTRAL, "Not printed, or not readable in this copy"))

    date, off_date = res.fields.get("doc_date"), official["doc_date"] if official else None
    if res.verdict != VERIFIED_EXACT and date and off_date:
        rows.append(_row("Issue date", OK if date == off_date else BAD,
                         f"Matches the official ({date})" if date == off_date else f"Official {off_date}; this copy {date}"))

    if m and res.verdict != VERIFIED_EXACT:
        if m.text is not None:
            changes = m.text.changes
            critical = [c for c in changes if _critical(c)]
            if not changes:
                rows.append(_row("Text", OK, "Same wording as the official"))
            else:
                rows.append(_row("Text", BAD, f"{len(changes)} change(s), {len(critical)} to dates, numbers, amounts, "
                                              "links or meaning" if critical else f"{len(changes)} change(s)"))
        else:
            rows.append(_row("Text", NEUTRAL, "Not compared (no readable text in this copy)"))
        compared = [v for v in m.visual if v.same_document]
        if compared:
            regions = sum(len(v.boxes) for v in compared)
            lines = sum(len(v.lines_to_check) for v in compared)
            if res.verdict == UNCONFIRMED_MATCH and lines and not regions:
                which = "one line differs" if lines == 1 else f"{lines} lines differ"
                rows.append(_row("Page image", WARN, f"No changed regions, but {which} slightly all along; "
                                                     "compare it with the official file"))
            elif res.verdict == UNCONFIRMED_MATCH:
                rows.append(_row("Page image", WARN, "No changed regions found, but a photo cannot rule out a small "
                                                     "edit such as a changed digit"))
            else:
                rows.append(_row("Page image", BAD if regions else OK,
                                 f"{regions} changed region(s)" if regions else "No changed regions"))
        elif m.visual:
            rows.append(_row("Page image", NEUTRAL, "Could not be lined up with the official page"))

    if res.signatures:
        good = all(s.get("intact") and s.get("covers_whole_file") for s in res.signatures)
        trusted = all(s.get("trusted") for s in res.signatures)
        rows.append(_row("Digital signature", OK if good else BAD,
                         ("Valid, covers the whole file" + ("" if trusted else "; signer's chain not checked"))
                         if good else "Broken, or content was added after signing"))
    elif res.kind == "pdf":
        rows.append(_row("Digital signature", NEUTRAL, "None"))

    if res.links:
        kinds = {link["kind"] for link in res.links}
        if "lookalike" in kinds:
            rows.append(_row("Links and QR codes", BAD, "A link imitates the issuer's website"))
        elif kinds & {"shortener", "unknown"}:
            rows.append(_row("Links and QR codes", WARN, "Links to sites the issuer does not use"))
        else:
            rows.append(_row("Links and QR codes", OK, "Only official, government or known sites"))

    if res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT, UNCONFIRMED_MATCH, ALTERED) and m:
        if res.withdrawn:
            rows.append(_row("Still current", WARN, f"Withdrawn from ugc.gov.in on {res.withdrawn}"))
        elif res.archived_only is not None:
            rows.append(_row("Still current", WARN, "No longer listed on ugc.gov.in"))
        elif getattr(res, "updated_by", None):
            u = res.updated_by[0]
            rows.append(_row("Still current", WARN, f"A later document may update it: {u['title']}"))
        else:
            rows.append(_row("Still current", OK, "Listed on ugc.gov.in"))
    return rows
