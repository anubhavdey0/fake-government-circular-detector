"""File-level signals from a submitted PDF or image.

These are context, not a verdict: genuine files are re-saved and printed all
the time. They become useful next to what the issuer itself does ("none of
UGC's 6,700 PDFs was made with Canva") and next to the official copy ("the
official file was produced by a scanner; this one by an online PDF editor and
saved twice since").
"""

from __future__ import annotations

import io
import re
from collections import Counter

_ANNOT = re.compile(rb"/Subtype\s*/(FreeText|Stamp|Square|Ink|Text|Highlight|Redact)\b")
_EDITORS = re.compile(r"ilovepdf|smallpdf|sejda|pdfescape|pdf-?xchange|foxit|nitro|canva|wps|"
                      r"pdfelement|pdffiller|soda ?pdf|camscanner|picsart|photoshop|illustrator|coreldraw|"
                      r"gimp|inkscape|adobe express", re.I)
# Image editors, as named in a picture's EXIF "Software" tag or XMP "CreatorTool".
_IMAGE_EDITORS = re.compile(r"photoshop|canva|picsart|snapseed|gimp|lightroom|pixlr|fotor|photoroom|"
                            r"adobe express|inshot|polarr|photo ?editor|paint\.net|krita|affinity|illustrator|"
                            r"coreldraw|remini|meitu|b612|vsco|photodirector|inkscape", re.I)
_XMP_TOOL = re.compile(rb"CreatorTool(?:=\"|>)([^\"<]{1,120})")


def tool_family(tool: str | None) -> str | None:
    """``Microsoft® Word 2016`` -> ``microsoft word``; ``doPDF Ver 7.3 Build 382`` -> ``dopdf``."""
    if not tool:
        return None
    t = re.sub(r"[\u00ae\u2122\u00a9\ufffd]", " ", tool)
    t = re.sub(r"\(.*?\)|https?://\S+", " ", t)
    t = re.sub(r"(?i)\b(?:v(?:er(?:sion)?)?\.?\s*)?\d[\w.\-]*", " ", t)
    words = [w for w in re.split(r"[^A-Za-z]+", t) if w and w.lower() not in ("build", "ver", "for", "with")]
    return " ".join(words[:3]).lower() or None


def image_signals(data: bytes) -> dict:
    """Software, camera and date from a picture's EXIF and XMP metadata (often stripped by chat apps)."""
    out: dict = {}
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
        for tag, key in ((0x0131, "software"), (0x010F, "make"), (0x0110, "model"), (0x0132, "taken")):
            value = exif.get(tag)
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            if value and str(value).strip():
                out[key] = str(value).strip()[:120]
        if "software" not in out and isinstance(img.info.get("Software"), str):
            out["software"] = img.info["Software"][:120]
        xmp = img.info.get("xmp") or img.info.get("XML:com.adobe.xmp")
        if isinstance(xmp, str):
            xmp = xmp.encode("utf-8", "replace")
        m = _XMP_TOOL.search(xmp or b"")
        if m:
            out["creator_tool"] = m.group(1).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - metadata is optional context
        pass
    return out


def image_notes(signals: dict) -> list[str]:
    tool = " ".join(filter(None, [signals.get("software"), signals.get("creator_tool")]))
    if tool and _IMAGE_EDITORS.search(tool):
        return [f"The picture's metadata says it was saved by an image editor ({tool}). Screenshots and phone "
                "photos of a notice do not normally pass through one."]
    return []


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


def compare_signals(submitted: dict, official: dict | None, usage: Counter | None = None,
                    total: int = 0, issuer: str = "UGC") -> list[str]:
    """``usage`` counts the tool families in the issuer's own PDFs, so a tool the issuer itself uses
    (UGC made 101 of its PDFs with iLovePDF) is not reported as suspicious."""
    notes = []
    tool = " ".join(filter(None, [submitted.get("producer"), submitted.get("creator")]))
    if tool and _EDITORS.search(tool):
        families = {tool_family(submitted.get("producer")), tool_family(submitted.get("creator"))} - {None}
        uses = max((usage or {}).get(f, 0) for f in families) if families else 0
        if usage is None:
            notes.append(f"Submitted PDF was last written by an editing tool ({tool}).")
        elif uses == 0:
            notes.append(f"The PDF was written by {tool}, an editing tool that none of {issuer}'s {total:,} own "
                         "PDFs was made with.")
        else:
            notes.append(f"The PDF was written by an editing tool ({tool}), but {issuer} itself made {uses:,} of its "
                         f"{total:,} PDFs with it, so on its own this means little.")
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
