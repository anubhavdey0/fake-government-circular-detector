"""Fetch a document from a link someone pasted, safely.

Forwarded circulars often arrive as a link rather than a file. A link on ugc.gov.in that the index
already holds is answered from the stored file without any download. Any other link is fetched
once, as a browser would, with guards suited to a server that fetches on request: http(s) only,
no private, loopback or link-local addresses (checked again at every redirect), a size cap, a time
limit, and only PDFs and images are accepted.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .config import USER_AGENT
from .extract import sniff_kind

MAX_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 4
TIMEOUT = 25.0


class LinkError(ValueError):
    """A link that cannot or should not be fetched; the message is shown to the user."""


def _check_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise LinkError("Only http and https links can be checked.")
    host = parts.hostname
    if not host:
        raise LinkError("That does not look like a web address.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LinkError(f"{host} could not be found.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise LinkError("That address points to a private or local network, which is not checked.")


def fetch_document(url: str, *, transport: httpx.BaseTransport | None = None,
                   check_host=_check_host) -> tuple[bytes, str]:
    """``(bytes, final URL)`` of a PDF or image at ``url``. Raises LinkError with a readable reason."""
    url = url.strip()
    if not urlsplit(url).scheme:
        url = "https://" + url
    with httpx.Client(timeout=httpx.Timeout(TIMEOUT, connect=10.0), follow_redirects=False, transport=transport,
                      headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_host(url)
            try:
                with client.stream("GET", url) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code >= 400:
                        raise LinkError(f"The link answered with an error (HTTP {resp.status_code}).")
                    length = resp.headers.get("content-length")
                    if length and length.isdigit() and int(length) > MAX_BYTES:
                        raise LinkError("The file at that link is larger than 30 MB.")
                    data = b""
                    for chunk in resp.iter_bytes(64 * 1024):
                        data += chunk
                        if len(data) > MAX_BYTES:
                            raise LinkError("The file at that link is larger than 30 MB.")
            except httpx.HTTPError as exc:
                raise LinkError("The link could not be opened (it timed out or refused the connection).") from exc
            if sniff_kind(data[:2048]) is None:
                raise LinkError("The link leads to a web page, not a PDF or image. Open it, download the "
                                "document, and check the file instead.")
            return data, url
    raise LinkError("The link redirects too many times.")


def verify_link(verifier, url: str, *, fetch=fetch_document):
    """Check the document behind a link, and say whose website the link is on."""
    from .issuers.ugc import canonical_url
    from .links import assess, describe, host_of, registrable, LinkFinding

    db, store = verifier.db, verifier.store
    canonical = canonical_url(url.strip())
    row = db.conn.execute(
        "SELECT f.sha256 FROM fetches f WHERE f.url = ? AND f.status = 'ok' ORDER BY f.id DESC LIMIT 1",
        (canonical,)).fetchone() if canonical else None
    name = (urlsplit(url).path.rsplit("/", 1)[-1] or "linked document")[:120]
    if row and store.has(row[0]):
        res = verifier.verify(store.get(row[0]), name)
        res.notes.insert(0, "This link is one the issuer lists; the file it serves was already in the index, so "
                            "nothing was downloaded.")
        final = url
    else:
        data, final = fetch(url)
        res = verifier.verify(data, name)
    host = host_of(final) or host_of(url)
    if host:
        kind, imitates = assess(host, verifier.profile["domains"])
        finding = LinkFinding("text", url, host, registrable(host), kind, imitates)
        status = {"official": "ok", "government": "ok", "known": "ok", "lookalike": "bad", "shortener": "warn",
                  "unknown": "warn"}.get(kind, "neutral")
        value = {"official": f"On {host}, the issuer's own website", "government": f"On {host}, a government site",
                 "known": f"On {host}, a site the issuer's documents use",
                 "lookalike": f"On {host}, which imitates {imitates}",
                 "shortener": f"A shortened link ({host}) that hides where it leads",
                 "unknown": f"On {host}, not a government site and not used by the issuer"}.get(kind, host)
        res.evidence.insert(1, {"check": "Link you pasted", "status": status, "value": value})
        note = describe(finding)
        if note and kind != "official":
            res.signal_notes.insert(0, note.replace("It points to", "The link you pasted points to", 1))
    return res
