"""Verification receipts: a short, shareable record of a result.

A receipt is created only when someone asks for one. It stores the outcome
(verdict, the official document or debunk it matched, the list of changes,
the file's hash) and never the submitted document itself, so it can be
posted back into the group where a fake was circulating.
"""

from __future__ import annotations

import json
import secrets

from .db import Database
from .matcher import VerificationResult
from .signatures import describe as describe_signature


def receipt_payload(res: VerificationResult) -> dict:
    m = res.match
    official = None
    if m and m.primary:
        p = m.primary
        official = {"title": p.title, "published": p.published, "url": p.url, "source": p.source,
                    "issuer": p.issuer}
    changes = []
    if m and m.text:
        changes = [{"official": c.official, "submitted": c.submitted, "reasons": c.reasons} for c in m.text.changes[:50]]
    regions = sum(len(v.boxes) for v in (m.visual if m else []) if v.same_document)
    return {
        "verdict": res.verdict,
        "summary": res.summary,
        "file": {"name": res.filename, "sha256": res.sha256, "kind": res.kind},
        "official": official,
        "known_fake": res.known_fake,
        "withdrawn": res.withdrawn,
        "archived_only": res.archived_only,
        "evidence": res.evidence,
        "updated_by": res.updated_by[:4],
        "changes": changes,
        "changed_regions": regions,
        "signatures": [describe_signature(s) for s in res.signatures],
        "notes": res.notes,
        "signal_notes": res.signal_notes,
        "index": res.index,
    }


def create_receipt(db: Database, res: VerificationResult) -> str:
    rid = secrets.token_urlsafe(9)
    db.save_receipt(rid, sha256=res.sha256, filename=res.filename, verdict=res.verdict, summary=res.summary,
                    payload=json.dumps(receipt_payload(res), ensure_ascii=False))
    db.commit()
    return rid


def load_receipt(db: Database, rid: str) -> tuple[dict, str] | None:
    row = db.receipt(rid)
    if row is None:
        return None
    return json.loads(row["payload"]), row["created_at"]
