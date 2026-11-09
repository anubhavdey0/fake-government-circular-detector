"""University Grants Commission (ugc.gov.in).

The site has two kinds of listing:

* Notices and Circulars are rendered client-side from a JSON endpoint
  (``POST /Notices/Fill_Notices``, ``POST /Circulars/Fill_Circulars``) that
  returns the whole archive in one response.
* Press releases, regulations and guidelines are plain HTML tables.

Older records link to ``www.ugc.ac.in``, which now 301-redirects to
``www.ugc.gov.in``; links are normalised to the ``.gov.in`` host so that
the same file is not fetched twice.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .base import Link, Notice, parse_date

BASE = "https://www.ugc.gov.in/"
CANONICAL_HOST = "www.ugc.gov.in"
_ALIAS_HOSTS = {"www.ugc.gov.in", "ugc.gov.in", "www.ugc.ac.in", "ugc.ac.in"}
_OFFICIAL_SUFFIXES = (".ugc.gov.in", ".ugc.ac.in")
DOC_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")

API_SOURCES: dict[str, tuple[str, str, str]] = {
    # source: (endpoint, grid type, human listing page)
    "notices": ("Notices/Fill_Notices", "NoticeGrid", "Notices"),
    "circulars": ("Circulars/Fill_Circulars", "CircularGrid", "Circulars"),
}
TABLE_SOURCES: dict[str, str] = {
    "press-releases": "publication/ugc_pressrelease",
    "regulations": "regulations",
    "guidelines": "Guideline",
}
_ATTACHMENT_FIELDS = ("pdfnews", "pdfnews1", "pdfnews2", "pdfnews3", "pdfnews4", "pdfnews_Hindi")
_PATH_SAFE = "/()-_.~!$&'*+,;=:@%"


class UGC:
    slug = "ugc"
    name = "University Grants Commission"
    official_hosts = frozenset(_ALIAS_HOSTS)

    def sources(self) -> list[str]:
        return [*API_SOURCES, *TABLE_SOURCES]

    def is_official_url(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host in _ALIAS_HOSTS or host.endswith(_OFFICIAL_SUFFIXES)

    def list_notices(self, client: httpx.Client, sources: list[str] | None = None) -> Iterator[Notice]:
        for source in sources or self.sources():
            if source in API_SOURCES:
                endpoint, grid, page = API_SOURCES[source]
                resp = client.post(urljoin(BASE, endpoint), data={"ID": "0", "type": grid})
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("c") != "success":
                    raise RuntimeError(f"UGC {source} endpoint returned {payload.get('c')!r}")
                for item in payload.get("List") or []:
                    notice = parse_api_record(item, source, listing_url=urljoin(BASE, page))
                    if notice:
                        yield notice
            elif source in TABLE_SOURCES:
                url = urljoin(BASE, TABLE_SOURCES[source])
                resp = client.get(url)
                resp.raise_for_status()
                yield from parse_table_page(resp.text, source, listing_url=url)
            else:
                raise ValueError(f"unknown UGC source {source!r}; choose from {self.sources()}")


def canonical_url(href: str, base: str = BASE) -> str | None:
    """Absolute, normalised URL for a document link, or None if it is not a UGC document."""
    href = (href or "").strip().strip("'\"")
    if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
        return None
    absolute = urljoin(base, href)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").lower()
    if not (host in _ALIAS_HOSTS or host.endswith(_OFFICIAL_SUFFIXES)):
        return None
    path = quote(unquote(parts.path), safe=_PATH_SAFE)
    if not path.lower().endswith(DOC_EXTENSIONS):
        return None
    if host in _ALIAS_HOSTS:
        return urlunsplit(("https", CANONICAL_HOST, path, parts.query, ""))
    return urlunsplit((parts.scheme or "https", host, path, parts.query, ""))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_api_record(item: dict, source: str, listing_url: str | None = None) -> Notice | None:
    """Turn one record from the Notices/Circulars JSON into a Notice."""
    title_html = item.get("title") or ""
    desc_html = item.get("description") or ""
    title_soup = BeautifulSoup(title_html, "html.parser")

    links: dict[str, Link] = {}

    def add(url: str | None, label: str | None) -> None:
        if url and url not in links:
            links[url] = Link(url=url, label=label)

    anchors = title_soup.find_all("a", href=True)
    for ul in title_soup.find_all(["ul", "ol"]):
        ul.extract()
    for a in title_soup.find_all("a"):
        a.extract()
    outside_text = _clean(title_soup.get_text(" "))

    # Two shapes occur: the whole title is one link, or a plain-text title
    # followed by links whose text names each file ("English", "Annexure").
    if outside_text:
        title = outside_text
        for a in anchors:
            add(canonical_url(a["href"]), _clean(a.get_text(" ")) or None)
    else:
        title = _clean(anchors[0].get_text(" ")) if anchors else ""
        for i, a in enumerate(anchors):
            label = None if i == 0 else (_clean(a.get_text(" ")) or None)
            add(canonical_url(a["href"]), label)

    for a in BeautifulSoup(desc_html, "html.parser").find_all("a", href=True):
        add(canonical_url(a["href"]), _clean(a.get_text(" ")) or None)

    for i, fieldname in enumerate(_ATTACHMENT_FIELDS):
        value = (item.get(fieldname) or "").strip()
        if not value:
            continue
        url = canonical_url(value if "/" in value else f"pdfnews/{value}")
        label = "Hindi" if fieldname == "pdfnews_Hindi" else (None if i == 0 else f"Attachment {i}")
        add(url, label)

    if not title:
        title = _clean(BeautifulSoup(desc_html, "html.parser").get_text(" "))
    record_id = str(item.get("ID") or "").strip()
    if not record_id or not (title or links):
        return None
    return Notice(
        issuer="ugc",
        source=source,
        record_id=record_id,
        title=title or "(untitled)",
        published=parse_date(item.get("created") or ""),
        links=list(links.values()),
        listing_url=listing_url,
    )


_SKIP_CELL = {"view", "download", "click here", "pdf", "english", "hindi"}


def parse_table_page(html: str, source: str, listing_url: str | None = None) -> Iterator[Notice]:
    """Rows of the HTML tables used for press releases, regulations and guidelines."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.find(id="content") or soup
    for tr in content.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        links: dict[str, Link] = {}
        for a in tr.find_all("a", href=True):
            url = canonical_url(a["href"], base=listing_url or BASE)
            if url and url not in links:
                text = _clean(a.get_text(" "))
                label = text if text.lower() in {"english", "hindi"} else None
                links[url] = Link(url=url, label=label)
        if not links:
            continue
        texts = [_clean(c.get_text(" ")) for c in cells]
        published = next((d for d in (parse_date(t) for t in texts) if d), None)
        candidates = [
            t
            for t in texts
            if t
            and not t.isdigit()
            and t.lower() not in _SKIP_CELL
            and not re.fullmatch(r"(published on\s*)?[\d/.\- ]+", t, flags=re.I)
        ]
        title = max(candidates, key=len) if candidates else "(untitled)"
        first_url = next(iter(links))
        yield Notice(
            issuer="ugc",
            source=source,
            record_id=first_url,
            title=title,
            published=published,
            links=list(links.values()),
            listing_url=listing_url,
        )
