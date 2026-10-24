"""The user's decisions about issues (acknowledged, muted), kept across restarts.

Issues themselves are derived from snapshots and never stored; only these decisions are.
Keyed by issue fingerprint:

* **Acknowledged** hides an issue until it resolves, i.e. until a successful poll of its
  service no longer reports it. If it comes back later, it shows up again.
* **Muted** hides an issue until a point in time, even if it resolves and recurs meanwhile
  (for things that flap).

Reads come from request handlers and the snapshot store on the event loop; the database is
written from a worker thread, the in-memory copy only on the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mediahub.db.models import IssueStateRow
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.models.issues import Issue

log = get_logger(__name__)


@dataclass(frozen=True)
class IssueState:
    fingerprint: str
    source_key: str
    first_seen: datetime | None = None
    acknowledged_at: datetime | None = None
    muted_until: datetime | None = None

    def muted(self, now: datetime) -> bool:
        return self.muted_until is not None and self.muted_until > now

    def hides(self, now: datetime) -> bool:
        return self.acknowledged_at is not None or self.muted(now)


class IssueStates:
    def __init__(self, sessions: sessionmaker[Session] | None = None) -> None:
        self._sessions = sessions  # None = memory only (tests)
        self._states: dict[str, IssueState] = {}
        self._background: set[asyncio.Task[None]] = set()

    def load(self) -> None:
        """Read stored decisions (blocking; call at startup)."""
        if self._sessions is None:
            return
        with session_scope(self._sessions) as db:
            rows = db.scalars(select(IssueStateRow)).all()
            self._states = {
                r.fingerprint: IssueState(
                    fingerprint=r.fingerprint,
                    source_key=r.source_key,
                    first_seen=r.first_seen,
                    acknowledged_at=r.acknowledged_at,
                    muted_until=r.muted_until,
                )
                for r in rows
            }

    def get(self, fingerprint: str) -> IssueState | None:
        return self._states.get(fingerprint)

    def apply(self, issue: Issue, now: datetime) -> Issue:
        """The issue annotated with the user's decision, if one currently applies."""
        state = self._states.get(issue.fingerprint)
        if state is None or not state.hides(now):
            return issue
        muted_until = state.muted_until if state.muted(now) else None
        return issue.model_copy(
            update={"acknowledged_at": state.acknowledged_at, "muted_until": muted_until}
        )

    # ----- user actions ----------------------------------------------------------------
    async def acknowledge(self, issue: Issue) -> None:
        await self._save(replace(self._state_for(issue), acknowledged_at=datetime.now(UTC)))

    async def mute(self, issue: Issue, until: datetime) -> None:
        await self._save(replace(self._state_for(issue), muted_until=until))

    async def restore(self, fingerprint: str) -> None:
        """Show the issue again (undo acknowledge and mute)."""
        if self._states.pop(fingerprint, None) is not None:
            await asyncio.to_thread(self._delete, [fingerprint])

    def _state_for(self, issue: Issue) -> IssueState:
        return self._states.get(issue.fingerprint) or IssueState(
            fingerprint=issue.fingerprint,
            source_key=issue.source.key,
            first_seen=issue.first_seen,
        )

    async def _save(self, state: IssueState) -> None:
        await asyncio.to_thread(self._write, state)
        self._states[state.fingerprint] = state

    # ----- resolution (called by the snapshot store) -----------------------------------
    def resolve_missing(self, source_key: str, present: set[str], now: datetime) -> None:
        """A successful poll of ``source_key`` reported only ``present``: the rest of its
        issues are resolved. Also tidies away expired mutes."""
        self.resolve(
            [
                fp
                for fp, state in self._states.items()
                if state.source_key == source_key and (fp not in present or not state.hides(now))
            ],
            now,
        )

    def resolve(self, fingerprints: list[str], now: datetime) -> None:
        """These issues are resolved: drop their acknowledgement; a live mute stays."""
        drop: list[str] = []
        for fp in fingerprints:
            state = self._states.get(fp)
            if state is None:
                continue
            if not state.muted(now):
                drop.append(fp)
                del self._states[fp]
            elif state.acknowledged_at is not None:
                state = self._states[fp] = replace(state, acknowledged_at=None)
                self._in_background(self._write, state)
        if drop:
            self._in_background(self._delete, drop)

    def _fps_of(self, source_key: str) -> list[str]:
        return [fp for fp, s in self._states.items() if s.source_key == source_key]

    def forget_source(self, source_key: str) -> None:
        """The source was removed (e.g. its instance deleted)."""
        drop = self._fps_of(source_key)
        for fp in drop:
            del self._states[fp]
        if drop:
            self._in_background(self._delete, drop)

    # ----- persistence -----------------------------------------------------------------
    def _write(self, state: IssueState) -> None:
        if self._sessions is None:
            return
        with session_scope(self._sessions) as db:
            db.merge(
                IssueStateRow(
                    fingerprint=state.fingerprint,
                    source_key=state.source_key,
                    first_seen=state.first_seen,
                    acknowledged_at=state.acknowledged_at,
                    muted_until=state.muted_until,
                )
            )

    def _delete(self, fingerprints: list[str]) -> None:
        if self._sessions is None:
            return
        with session_scope(self._sessions) as db:
            db.execute(delete(IssueStateRow).where(IssueStateRow.fingerprint.in_(fingerprints)))

    def _in_background(self, fn: Callable[..., None], *args: object) -> None:
        if self._sessions is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            fn(*args)
            return

        async def run() -> None:
            try:
                await asyncio.to_thread(fn, *args)
            except Exception:
                log.exception("issue_state_write_failed")

        task = loop.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
