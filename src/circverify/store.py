"""Content-addressed storage for downloaded documents."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO, Iterable


class BlobStore:
    """Stores each file once, under its SHA-256, at ``files/ab/abcdef...``."""

    def __init__(self, root: Path):
        self.root = Path(root) / "files"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        return self.path(sha256).exists()

    def get(self, sha256: str) -> bytes:
        return self.path(sha256).read_bytes()

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self.path(sha)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        return sha

    def put_stream(self, chunks: Iterable[bytes], max_bytes: int) -> tuple[str, int, bytes]:
        """Write chunks to the store, hashing as we go.

        Returns ``(sha256, size, first_bytes)``. Raises ``FileTooLarge`` once
        ``max_bytes`` is exceeded, leaving nothing behind.
        """
        hasher = hashlib.sha256()
        size = 0
        head = b""
        fd, tmp_name = tempfile.mkstemp(dir=self.root, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLarge(size)
                    if len(head) < 2048:
                        head += chunk[: 2048 - len(head)]
                    hasher.update(chunk)
                    fh.write(chunk)
            sha = hasher.hexdigest()
            self._commit(tmp_name, self.path(sha))
            return sha, size, head
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def open(self, sha256: str) -> BinaryIO:
        return self.path(sha256).open("rb")

    _move_lock = threading.Lock()

    def _commit(self, tmp_name: str, dest: Path) -> None:
        # Two downloads of identical bytes can finish at once; on Windows the
        # second os.replace onto a file being replaced fails, so serialise.
        with self._move_lock:
            if dest.exists():
                os.unlink(tmp_name)
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_name, dest)


class FileTooLarge(Exception):
    pass
