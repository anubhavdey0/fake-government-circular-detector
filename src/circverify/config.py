"""Paths and identifiers shared across the package."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__

APP_NAME = "circverify"
PROJECT_URL = "https://github.com/anubhavdey0/fake-government-circular-detector"
USER_AGENT = f"{APP_NAME}/{__version__} (+{PROJECT_URL})"

DATA_DIR_ENV = "CIRCVERIFY_DATA_DIR"

# Timestamps are stored in UTC; dates are shown in the issuer's time zone (UGC: India),
# wherever the server runs. A sync at 01:43 IST on 10 Nov is "10 Nov", not "9 Nov".
ISSUER_TZ = timezone(timedelta(hours=5, minutes=30))


def issuer_date(iso: str | None) -> datetime | None:
    """A stored timestamp or date as a datetime in the issuer's time zone; plain dates stay as they are."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = datetime.fromisoformat(iso[:19])
        except ValueError:
            return None
    return dt.astimezone(ISSUER_TZ) if dt.tzinfo is not None else dt


def default_data_dir() -> Path:
    """Where the index and downloaded files live.

    The crawl stores several GB of PDFs, so the default is the per-user
    application data folder rather than the working directory (which is
    often inside a synced folder such as OneDrive or Dropbox).
    """
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME
