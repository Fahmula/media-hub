"""Small on-disk LRU cache for proxied images (``/config/cache/images``).

Keys include the upstream image tag, which changes whenever the image does, so entries never
need invalidating, only evicting. Each file is ``<content-type>\\n<bytes>``. Blocking file I/O:
call from a thread.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path

DEFAULT_MAX_BYTES = 200 * 1024 * 1024


class ImageCache:
    def __init__(self, directory: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.directory = directory
        self.max_bytes = max_bytes

    def _path(self, key: str) -> Path:
        return self.directory / hashlib.sha256(key.encode()).hexdigest()

    def get(self, key: str) -> tuple[bytes, str] | None:
        path = self._path(key)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        content_type, sep, data = raw.partition(b"\n")
        if not sep or not content_type.startswith(b"image/"):
            return None
        with contextlib.suppress(OSError):
            os.utime(path)  # mtime doubles as "last used" for eviction
        return data, content_type.decode()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content_type.encode() + b"\n" + data)
            os.replace(tmp, self._path(key))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return
        self._evict()

    def _evict(self) -> None:
        entries = []
        with contextlib.suppress(OSError):
            for entry in os.scandir(self.directory):
                if entry.is_file() and not entry.name.startswith(".tmp-"):
                    stat = entry.stat()
                    entries.append((stat.st_mtime, stat.st_size, entry.path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            with contextlib.suppress(OSError):
                os.unlink(path)
                total -= size
