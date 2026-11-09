"""Signed PDFs, made here with a throwaway self-signed certificate."""

from __future__ import annotations

import datetime as dt
import io

import pytest

from conftest import OFFICIAL
from pdfgen import make_pdf

pyhanko = pytest.importorskip("pyhanko")
crypto = pytest.importorskip("cryptography")

from asn1crypto import keys as asn1_keys  # noqa: E402
from asn1crypto import x509 as asn1_x509  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from pyhanko.pdf_utils import generic  # noqa: E402
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: E402
from pyhanko.sign import signers  # noqa: E402
from pyhanko_certvalidator.registry import SimpleCertificateStore  # noqa: E402

from circverify.signatures import describe, pdf_signatures  # noqa: E402


@pytest.fixture(scope="module")
def signed_pdf() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Office")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    signer = signers.SimpleSigner(
        signing_cert=asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)),
        signing_key=asn1_keys.PrivateKeyInfo.load(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        cert_registry=SimpleCertificateStore())
    w = IncrementalPdfFileWriter(io.BytesIO(make_pdf([OFFICIAL])), strict=False)
    out = signers.sign_pdf(w, signers.PdfSignatureMetadata(field_name="Signature1"), signer=signer)
    return out.getvalue()


def test_intact_signature_covers_the_whole_file(signed_pdf):
    [sig] = pdf_signatures(signed_pdf)
    assert sig["intact"] and sig["covers_whole_file"] and sig["signer"] == "Test Signer"
    assert "intact and covers the whole file" in describe(sig)


def test_edit_after_signing_is_detected(signed_pdf):
    w = IncrementalPdfFileWriter(io.BytesIO(signed_pdf), strict=False)
    w.root["/EditedBy"] = generic.pdf_string("someone")
    w.update_root()
    out = io.BytesIO()
    w.write(out)
    [sig] = pdf_signatures(out.getvalue())
    assert sig["intact"] and not sig["covers_whole_file"]
    assert "added to the file after it was signed" in describe(sig)


def test_tampered_bytes_break_the_signature(signed_pdf):
    data = bytearray(signed_pdf)
    i = data.find(b"fellowship scheme")
    data[i] = ord("F")
    [sig] = pdf_signatures(bytes(data))
    assert not sig["intact"] and "broken" in describe(sig)


def test_unsigned_pdf_has_no_signatures():
    assert pdf_signatures(make_pdf([OFFICIAL])) == []
