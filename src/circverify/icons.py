"""Inline SVG icons from Phosphor Icons (regular weight, MIT licence, see static/icons/LICENSE)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=None)
def _svg(name: str) -> str:
    return (files("circverify") / "static" / "icons" / f"{name}.svg").read_text(encoding="utf-8").strip()


def icon(name: str, cls: str = "icon") -> str:
    """An icon that inherits the text colour. Decorative: hidden from assistive tech."""
    return _svg(name).replace("<svg ", f'<svg class="{cls}" aria-hidden="true" focusable="false" ', 1)


def favicon_svg(color: str = "#1f43c4") -> str:
    return _svg("seal-check").replace('fill="currentColor"', f'fill="{color}"', 1)
