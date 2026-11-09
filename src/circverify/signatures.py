"""Embedded digital signatures (PAdES / PKCS#7) in submitted PDFs, checked with pyHanko.

A valid signature that covers the whole file is the strongest evidence a
PDF can carry: any edit after signing breaks it. What this module reports:

* **intact**: the signed bytes have not changed since signing;
* **covers_whole_file**: nothing was appended after the signature (an
  incremental update after signing is how signed PDFs get edited);
* **signer** and **issuer** from the certificate.

It does not claim legal trust. Indian signing certificates chain to the
Controller of Certifying Authorities (CCA India) roots, which are not
bundled; ``trusted`` is therefore usually False and the report says so.
Pass ``trust_roots`` (DER/PEM files) to check the chain as well.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path


def _quiet() -> None:
    for name in ("pyhanko", "pyhanko_certvalidator", "pyhanko.sign.validation"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def pdf_signatures(data: bytes, trust_roots: list[Path] | None = None) -> list[dict]:
    """One dict per embedded signature; an empty list if there are none or pyHanko is missing."""
    if b"/ByteRange" not in data:
        return []
    try:
        from asn1crypto import x509 as asn1_x509
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation.errors import SignatureValidationError
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko.sign.validation.status import SignatureCoverageLevel
        from pyhanko_certvalidator import ValidationContext
    except ImportError:
        return []
    _quiet()
    roots = []
    for path in trust_roots or []:
        raw = Path(path).read_bytes()
        if raw.lstrip().startswith(b"-----BEGIN"):
            from asn1crypto import pem
            raw = pem.unarmor(raw)[2]
        roots.append(asn1_x509.Certificate.load(raw))
    try:
        reader = PdfFileReader(io.BytesIO(data), strict=False)
        embedded = list(reader.embedded_signatures)
    except Exception:  # noqa: BLE001 - a damaged signature dictionary is reported as no signature
        return []
    out = []
    for sig in embedded:
        entry: dict = {"field": sig.field_name}
        try:
            cert = sig.signer_cert
            subj, iss = cert.subject.native, cert.issuer.native
            entry.update(signer=subj.get("common_name"), organisation=subj.get("organization_name"),
                         issuer=iss.get("common_name"))
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx = ValidationContext(trust_roots=roots or None, allow_fetching=False)
            status = validate_pdf_signature(sig, ctx)
            entry.update(
                intact=bool(status.intact),
                valid=bool(status.valid),
                trusted=bool(status.trusted),
                covers_whole_file=status.coverage == SignatureCoverageLevel.ENTIRE_FILE,
                signed_at=status.signer_reported_dt.isoformat() if status.signer_reported_dt else None,
            )
        except (SignatureValidationError, ValueError, KeyError) as exc:
            entry.update(intact=False, valid=False, trusted=False, covers_whole_file=False, error=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            entry.update(intact=False, valid=False, trusted=False, covers_whole_file=False,
                         error=f"{type(exc).__name__}: {str(exc)[:160]}")
        out.append(entry)
    return out


def describe(sig: dict) -> str:
    """One plain sentence about a signature, for the report."""
    who = sig.get("signer") or "an unnamed signer"
    org = f" ({sig['organisation']})" if sig.get("organisation") else ""
    when = f" on {sig['signed_at'][:10]}" if sig.get("signed_at") else ""
    if not sig.get("intact"):
        return f"Digital signature by {who}{org}{when} is broken: the signed content has changed since signing."
    if not sig.get("covers_whole_file"):
        return (f"Digital signature by {who}{org}{when} is intact, but content was added to the file after it "
                "was signed; that later part is not covered by the signature.")
    trust = ("and the certificate chain is trusted" if sig.get("trusted") else
             "(certificate issued by " + (sig.get("issuer") or "an unknown authority") +
             "; the CCA India root was not checked)")
    return f"Digitally signed by {who}{org}{when}. The signature is intact and covers the whole file {trust}."
