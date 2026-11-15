"""Pack the local index into one archive, to move it to a server (deploy/oracle/restore-data.sh).

The database is copied with SQLite's backup API, so the archive is consistent even while the web
app or a crawl is using it. Stored files go in as they are (PDFs and images are already compressed).
Evaluation caches and old backups are left out.

    python scripts/pack_data.py circverify-data.tar
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

from circverify.config import default_data_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="archive to write, e.g. circverify-data.tar")
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args()

    root = args.data_dir or default_data_dir()
    db = root / "index.sqlite3"
    if not db.exists():
        print(f"No index at {db}", file=sys.stderr)
        return 1
    files = sorted(p for p in (root / "files").rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    print(f"Index at {root}: database {db.stat().st_size / 1e6:.0f} MB, {len(files):,} files, {size / 1e9:.2f} GB")

    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "index.sqlite3"
        src, dst = sqlite3.connect(db), sqlite3.connect(snap)
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal beside it
        dst.close()
        src.close()
        with tarfile.open(args.out, "w") as tar:
            tar.add(snap, arcname="index.sqlite3")
            for i, p in enumerate(files, 1):
                tar.add(p, arcname=p.relative_to(root).as_posix())
                if i % 1000 == 0:
                    print(f"  {i:,}/{len(files):,} files")
    print(f"Wrote {args.out} ({args.out.stat().st_size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
