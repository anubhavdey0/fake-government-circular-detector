"""Embedded digital signatures (PAdES / PKCS#7) in submitted PDFs, checked with pyHanko.

A valid signature that covers the whole file is the strongest evidence a
PDF can carry: any edit after signing breaks it. What this module reports:

* **intact**: the signed bytes have not changed since signing;
* **covers_whole_file**: nothing was appended after the signature (an
  incremental update after signing is how signed PDFs get edited);
* **signer** and **issuer** from the certificate.

* **trusted**: the certificate chain leads to a root of the India PKI (the
  Controller of Certifying Authorities, CCA India), checked as of the signing
  time the signature states.

The CCA India roots and a few licensed-CA intermediates are bundled in
``trust/`` (see its README for where each came from and how it was checked).
Revocation is not checked, since that needs the network. More roots can be
listed in ``CIRCVERIFY_TRUST_ROOTS`` (DER/PEM files, separated like PATH), or
passed as ``trust_roots``. The operating system's trust store is deliberately
not used, so the answer is the same on every machine.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from functools import lru_cache
from pathlib import Path

TRUST_DIR = Path(__file__).parent / "trust"


def _quiet() -> None:
    for name in ("pyhanko", "pyhanko_certvalidator", "pyhanko.sign.validation"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _load_cert(path: Path):
    from asn1crypto import pem
    from asn1crypto import x509 as asn1_x509

    raw = Path(path).read_bytes().strip()
    if raw.startswith(b"-----BEGIN"):
        raw = pem.unarmor(raw)[2]
    elif raw.startswith(b"MII"):  # base64 without the PEM header, as some CA sites serve it
        raw = base64.b64decode(b"".join(raw.split()))
    return asn1_x509.Certificate.load(raw)


@lru_cache(maxsize=1)
def bundled() -> tuple[tuple, tuple]:
    """(roots, intermediates) shipped in ``trust/``."""
    def load(sub: str) -> tuple:
        return tuple(_load_cert(p) for p in sorted((TRUST_DIR / sub).glob("*.pem")))
    return load("roots"), load("intermediates")


def pdf_signatures(data: bytes, trust_roots: list[Path] | None = None) -> list[dict]:
    """One dict per embedded signature; an empty list if there are none or pyHanko is missing.

    ``trust_roots`` replaces the bundled CCA India roots (an empty list checks no chain at all)."""
    if b"/ByteRange" not in data:
        return []
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation.errors import SignatureValidationError
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko.sign.validation.status import SignatureCoverageLevel
        from pyhanko_certvalidator import ValidationContext
    except ImportError:
        return []
    _quiet()
    intermediates: list = []
    if trust_roots is None:
        roots, extra = bundled()
        roots, intermediates = list(roots), list(extra)
        roots += [_load_cert(Path(x)) for x in os.environ.get("CIRCVERIFY_TRUST_ROOTS", "").split(os.pathsep) if x]
    else:
        roots = [_load_cert(Path(x)) for x in trust_roots]
    try:
        reader = PdfFileReader(io.BytesIO(data), strict=False)
        embedded = list(reader.embedded_signatures)
    except Exception:  # noqa: BLE001 - a damaged signature dictionary is reported as no signature
        return []
    out = []
    for sig in embedded:
        entry: dict = {"field": sig.field_name, "roots_checked": bool(roots)}
        try:
            cert = sig.signer_cert
            subj, iss = cert.subject.native, cert.issuer.native
            entry.update(signer=subj.get("common_name"), organisation=subj.get("organization_name"),
                         issuer=iss.get("common_name"))
        except Exception:  # noqa: BLE001
            pass
        try:
            # Certificates expire: check the chain as of when the document says it was signed.
            moment = getattr(sig, "self_reported_timestamp", None)
            ctx = ValidationContext(trust_roots=roots, other_certs=intermediates, allow_fetching=False,
                                    moment=moment)
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
    if sig.get("valid") is False and sig.get("covers_whole_file"):
        return (f"Digital signature by {who}{org}{when}: the signed content is unchanged, but the signature value "
                "could not be verified against the signer's certificate, so it does not confirm who signed it.")
    if not sig.get("covers_whole_file"):
        return (f"Digital signature by {who}{org}{when} is intact, but content was added to the file after it "
                "was signed; that later part is not covered by the signature.")
    issuer = sig.get("issuer") or "an unknown authority"
    if sig.get("trusted"):
        trust = (f"and the certificate (issued by {issuer}) chains to a root of the India PKI, checked as of the "
                 "signing date; revocation was not checked")
    elif sig.get("roots_checked"):
        trust = (f"(certificate issued by {issuer}), but its chain to a CCA India root could not be confirmed "
                 "(an intermediate certificate may be missing), so who signed it is not confirmed")
    else:
        trust = f"(certificate issued by {issuer}; the CCA India root was not checked)"
    return f"Digitally signed by {who}{org}{when}. The signature is intact and covers the whole file {trust}."
