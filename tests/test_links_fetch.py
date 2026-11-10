"""Checking a document from a pasted link: indexed UGC links, fetched links, and refused addresses."""

from __future__ import annotations

import httpx
import pytest

from conftest import OFFICIAL
from pdfgen import make_pdf

from circverify.fetchurl import LinkError, _check_host, fetch_document, verify_link
from circverify.matcher import NO_MATCH, VERIFIED_EXACT, Verifier

FAKE = make_pdf([[*OFFICIAL[:3], "Subject: Offline examinations in all universities",
                  "All universities are hereby advised to conduct the coming semester",
                  "examinations in offline mode at the home centres of the students.", "(Secretary)"]])


def _no_dns(url):  # tests never touch the network
    return None


def test_indexed_ugc_link_is_answered_from_the_store(index):
    db, store, _ = index
    v = Verifier(db, store, use_ocr=False)
    res = verify_link(v, "https://www.ugc.gov.in/pdfnews/1001.pdf",
                      fetch=lambda url: pytest.fail("an indexed link must not be downloaded"))
    assert res.verdict == VERIFIED_EXACT and "nothing was downloaded" in res.notes[0]
    assert res.evidence[1]["check"] == "Link you pasted" and res.evidence[1]["status"] == "ok"


def test_lookalike_link_is_fetched_and_flagged(index):
    db, store, _ = index
    v = Verifier(db, store, use_ocr=False)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=FAKE))
    fetch = lambda url: fetch_document(url, transport=transport, check_host=_no_dns)  # noqa: E731
    res = verify_link(v, "https://ugc-gov-in.com/notice.pdf", fetch=fetch)
    assert res.verdict == NO_MATCH
    assert res.evidence[1]["status"] == "bad" and "imitates ugc.gov.in" in res.evidence[1]["value"]
    assert res.signal_notes[0].startswith("The link you pasted points to")


def test_redirects_web_pages_and_size_are_handled():
    def respond(request):
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/file.pdf"})
        if request.url.path == "/file.pdf":
            return httpx.Response(200, content=FAKE)
        return httpx.Response(200, text="<!DOCTYPE html><html>a page</html>")

    t = httpx.MockTransport(respond)
    data, final = fetch_document("https://example.org/go", transport=t, check_host=_no_dns)
    assert data == FAKE and final.endswith("/file.pdf")
    with pytest.raises(LinkError, match="web page"):
        fetch_document("https://example.org/page", transport=t, check_host=_no_dns)
    with pytest.raises(LinkError, match="http"):
        fetch_document("ftp://example.org/x.pdf", transport=t, check_host=_check_host)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/x.pdf", "http://localhost/x.pdf", "http://10.0.0.5/x.pdf",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/x.pdf"])
def test_private_addresses_are_refused(url):
    with pytest.raises(LinkError, match="private or local"):
        _check_host(url)
