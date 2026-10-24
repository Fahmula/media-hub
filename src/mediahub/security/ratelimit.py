"""Tiny in-memory sliding-window rate limiter (single process by design)."""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        if len(self._hits) >= self._max_keys:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window_s}
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
