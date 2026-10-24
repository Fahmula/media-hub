"""Tracks whether anyone is looking, so pollers can run fast only when it matters.

Browser page/partial requests and peer snapshot requests call :meth:`touch`. When demand
goes from idle to active, sleeping pollers are woken immediately so the first screen a user
sees after a quiet period is fresh, not up to an idle-interval old.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class DemandTracker:
    def __init__(self, active_window_s: float = 120.0) -> None:
        self.active_window_s = active_window_s
        self._last = float("-inf")
        self._listeners: list[Callable[[], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def on_wake(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def is_active(self) -> bool:
        return time.monotonic() - self._last < self.active_window_s

    def touch(self) -> None:
        was_active = self.is_active()
        self._last = time.monotonic()
        if not was_active:
            self._signal()

    def _signal(self) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._fire)  # touched from a threadpool route
        else:
            self._fire()

    def _fire(self) -> None:
        for callback in self._listeners:
            callback()
