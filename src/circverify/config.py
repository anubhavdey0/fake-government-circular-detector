"""Paths and identifiers shared across the package."""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__

APP_NAME = "circverify"
PROJECT_URL = "https://github.com/anubhavdey0/fake-government-circular-detector"
USER_AGENT = f"{APP_NAME}/{__version__} (+{PROJECT_URL})"

DATA_DIR_ENV = "CIRCVERIFY_DATA_DIR"


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
