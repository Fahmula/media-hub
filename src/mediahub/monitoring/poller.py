"""Background polling.

One long-lived task per target (service instance or peer). A target's polls never overlap:
its loop is the only caller, and ``poll_now`` takes the same lock. A global semaphore bounds
how many polls run at once. Intervals are short while someone is looking (see
``DemandTracker``) and relaxed otherwise; failures back off exponentially up to 5 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod

from mediahub.logging import get_logger
from mediahub.monitoring.demand import DemandTracker

log = get_logger(__name__)

IDLE_INTERVAL_S = 60.0
MAX_BACKOFF_S = 300.0
POLL_TIMEOUT_S = 30.0


class PollTarget(ABC):
    key: str

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()

    @abstractmethod
    def interval(self, active: bool) -> float: ...

    @abstractmethod
    async def run_once(self, active: bool) -> bool:
        """Poll once and publish the result. Returns False on failure. Must not raise."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


def backoff_delay(base: float, failures: int) -> float:
    if failures <= 0:
        return base
    return min(max(base, 5.0) * 2 ** (failures - 1), MAX_BACKOFF_S)


class Poller:
    def __init__(self, demand: DemandTracker, max_concurrency: int = 8) -> None:
        self._demand = demand
        self._sem = asyncio.Semaphore(max_concurrency)
        self._targets: dict[str, PollTarget] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        demand.on_wake(self.wake_all)

    @property
    def keys(self) -> set[str]:
        return set(self._targets)

    def wake_all(self) -> None:
        for target in self._targets.values():
            target.wake.set()

    async def add(self, target: PollTarget) -> None:
        await self.remove(target.key)
        self._targets[target.key] = target
        self._tasks[target.key] = asyncio.create_task(self._loop(target), name=f"poll:{target.key}")

    async def remove(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        target = self._targets.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if target is not None:
            await target.aclose()

    async def poll_now(self, key: str) -> bool | None:
        target = self._targets.get(key)
        if target is None:
            return None
        return await self._run(target)

    async def stop(self) -> None:
        for key in list(self._targets):
            await self.remove(key)

    async def _run(self, target: PollTarget) -> bool:
        active = self._demand.is_active()
        async with target.lock, self._sem:
            try:
                return await asyncio.wait_for(target.run_once(active), POLL_TIMEOUT_S)
            except TimeoutError:
                log.warning("poll_timeout", target=target.key)
                return False

    async def _loop(self, target: PollTarget) -> None:
        failures = 0
        while True:
            try:
                ok = await self._run(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll_crashed", target=target.key)
                ok = False
            failures = 0 if ok else failures + 1
            delay = backoff_delay(target.interval(self._demand.is_active()), failures)
            target.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(target.wake.wait(), timeout=delay)
