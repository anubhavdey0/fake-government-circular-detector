"""Issuer adapters. Each one knows how to list everything a single issuer has published."""

from __future__ import annotations

from .base import Issuer, Link, Notice
from .ugc import UGC

ISSUERS: dict[str, Issuer] = {"ugc": UGC()}


def get_issuer(slug: str) -> Issuer:
    try:
        return ISSUERS[slug]
    except KeyError:
        raise KeyError(f"unknown issuer {slug!r}; available: {', '.join(ISSUERS)}") from None


__all__ = ["ISSUERS", "Issuer", "Link", "Notice", "get_issuer"]
