"""Single-process guard.

The poller and snapshot cache live in-process, so running several workers against the same
``/config`` would poll every service N times and serve inconsistent data. An exclusive
``fcntl`` lock on ``mediahub.lock`` makes a second process fail fast with a clear message.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise AlreadyRunningError(
                f"Another MediaHub process holds {self._path}. MediaHub must run as a single "
                "worker per config directory."
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
