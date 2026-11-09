"""What an issuer adapter has to provide."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator, Protocol

import httpx


@dataclass(frozen=True)
class Link:
    url: str
    label: str | None = None


@dataclass
class Notice:
    """One entry on an issuer's listing page, with the files it links to."""

    issuer: str
    source: str
    record_id: str
    title: str
    published: str | None
    links: list[Link] = field(default_factory=list)
    listing_url: str | None = None


class Issuer(Protocol):
    slug: str
    name: str
    official_hosts: frozenset[str]

    def sources(self) -> list[str]: ...

    def list_notices(self, client: httpx.Client, sources: list[str] | None = None) -> Iterator[Notice]: ...

    def is_official_url(self, url: str) -> bool: ...


_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_YMD = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def parse_date(text: str) -> str | None:
    """Find a date in ``text`` (``YYYY-MM-DD`` or ``DD/MM/YYYY``) and return it as ISO."""
    if not text:
        return None
    for rx, order in ((_YMD, "ymd"), (_DMY, "dmy")):
        m = rx.search(text)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        y, mth, d = (a, b, c) if order == "ymd" else (c, b, a)
        try:
            parsed = date(y, mth, d)
        except ValueError:
            continue
        if parsed.year < 1950:  # placeholder dates such as 1900-01-01
            return None
        return parsed.isoformat()
    return None
