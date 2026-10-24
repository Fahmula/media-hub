from __future__ import annotations

from pathlib import Path

import pytest

from mediahub.runtime_lock import AlreadyRunningError, InstanceLock


def test_second_lock_fails(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "x.lock")
    first.acquire()
    with pytest.raises(AlreadyRunningError):
        InstanceLock(tmp_path / "x.lock").acquire()
    first.release()
    again = InstanceLock(tmp_path / "x.lock")
    again.acquire()
    again.release()
