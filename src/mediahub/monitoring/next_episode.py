"""Opt-in next-episode health check (Emby option "Health-check the next episode").

Once an episode has played past the configured percentage, the next episode is found in Emby
and every InfiniDysk on this server is asked to health-check it (``POST /api/recheck-file``),
so a broken release is repaired or replaced before anyone gets to it.

* Each playing item is handled once (autoplay into the next episode is a new item).
* An InfiniDysk file asked for in the last 24 h isn't asked for again.
* Emby or InfiniDysk being unreachable, or the next episode's file not being resolved yet, is
  retried after 5 minutes.
* Only local Emby and InfiniDysk integrations take part: never demo integrations or peers.

An accepted request is then followed through InfiniDysk's read-only active checks ("being
checked", progress) and history (healthy, repaired, ...) until its result shows up, for at most
30 minutes. A result counts only if InfiniDysk recorded it after the request (a few seconds of
clock difference allowed), so an older check of the same file isn't taken for this one.

Outcomes go to the store for the stream card and are forgotten when the session ends.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from mediahub.integrations.base import Integration
from mediahub.integrations.emby.integration import EmbyIntegration, NextEpisode
from mediahub.integrations.errors import IntegrationError
from mediahub.integrations.infinidysk.integration import (
    ActiveCheck,
    CheckResult,
    InfiniDyskIntegration,
    RecheckOutcome,
)
from mediahub.logging import get_logger
from mediahub.models.playback import (
    MediaKind,
    NextCheckState,
    NextEpisodeCheck,
    PlaybackSession,
)
from mediahub.monitoring.poller import PollTarget
from mediahub.monitoring.store import SnapshotStore

log = get_logger(__name__)

CHECK_INTERVAL_S = 30.0
RECENT_S = 24 * 3600.0
RETRY_S = 300.0
FOLLOW_S = 30 * 60.0
CLOCK_SKEW = timedelta(seconds=5)
TARGET_KEY = "next-episode"

_ACCEPTED = {
    "queued": NextCheckState.QUEUED,
    "already-queued": NextCheckState.ALREADY_QUEUED,
    "already-running": NextCheckState.ALREADY_RUNNING,
}
# InfiniDysk's HealthCheckResult.RepairAction values, then HealthResult values.
_BY_REPAIR = {
    1: NextCheckState.REPAIRED,
    2: NextCheckState.DELETED,
    3: NextCheckState.ACTION_NEEDED,
    4: NextCheckState.REPAIRED_PAR2,
}
_BY_RESULT = {0: NextCheckState.HEALTHY, 2: NextCheckState.DEGRADED}

# (emby instance id, session id, item id)
SessionKey = tuple[str, str, str]


@dataclass
class FollowUp:
    """A requested check being followed until InfiniDysk records its result."""

    dav_id: UUID
    since: datetime  # when it was requested (wall clock, compared with InfiniDysk's history)
    until: float  # monotonic deadline
    check: NextEpisodeCheck  # what the card shows now
    series: str
    once: bool = False  # a check requested earlier: look once, keep following only if active


def finished_state(result: CheckResult) -> NextCheckState:
    return _BY_REPAIR.get(result.repair) or _BY_RESULT.get(result.result, NextCheckState.UNHEALTHY)


def dav_item_id(strm_id: str | None) -> UUID | None:
    """InfiniDysk's item id from a STRM target segment (``<guid>`` or ``<guid>.mkv``)."""
    if not strm_id:
        return None
    try:
        return UUID(strm_id.split(".", 1)[0])
    except ValueError:
        return None


class NextEpisodeTarget(PollTarget):
    key = TARGET_KEY

    def __init__(
        self,
        store: SnapshotStore,
        integrations: Callable[[], list[Integration]],
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._store = store
        self._integrations = integrations
        self._clock = clock
        self._wall = wall
        self._next_try: dict[SessionKey, float] = {}  # absent = not tried; inf = done
        # InfiniDysk item id -> when it was asked for (monotonic, wall clock)
        self._asked: dict[UUID, tuple[float, datetime]] = {}
        self._follow_ups: dict[SessionKey, FollowUp] = {}

    def interval(self, active: bool) -> float:
        return CHECK_INTERVAL_S

    async def run_once(self, active: bool) -> bool:
        integrations = self._integrations()
        embys = [
            i
            for i in integrations
            if isinstance(i, EmbyIntegration) and i.next_episode_percent is not None
        ]
        infinidysks = [i for i in integrations if isinstance(i, InfiniDyskIntegration)]
        now = self._clock()
        live: set[SessionKey] = set()
        if infinidysks:
            for emby in embys:
                snapshot = self._store.get_local(emby.config.id)
                if snapshot is None or not snapshot.status.is_up:
                    continue
                for session in snapshot.sessions:
                    key = (emby.config.id, session.session_id, session.item_id or "")
                    live.add(key)
                    if self._due(session, emby.next_episode_percent, key, now):
                        await self._handle(emby, infinidysks, session, key, now)
        # Sessions that ended, or whose option was turned off: forget their outcomes.
        self._store.drop_next_checks(self._next_try.keys() - live)
        self._next_try = {k: v for k, v in self._next_try.items() if k in live}
        self._follow_ups = {k: f for k, f in self._follow_ups.items() if k in live}
        self._asked = {k: t for k, t in self._asked.items() if now - t[0] < RECENT_S}
        if infinidysks and self._follow_ups:
            await self._follow(infinidysks, now)
        return True

    def _due(
        self, session: PlaybackSession, percent: int | None, key: SessionKey, now: float
    ) -> bool:
        return (
            percent is not None
            and session.media.kind is MediaKind.EPISODE
            and bool(session.item_id and session.user_id and session.series_id)
            and session.progress_pct is not None
            and session.progress_pct >= percent
            and now >= self._next_try.get(key, 0.0)
        )

    async def _handle(
        self,
        emby: EmbyIntegration,
        infinidysks: list[InfiniDyskIntegration],
        session: PlaybackSession,
        key: SessionKey,
        now: float,
    ) -> None:
        # Claimed up front, so a run cut short by the poll timeout waits before trying again.
        self._next_try[key] = now + RETRY_S
        check, retry, follow = await self._check(emby, infinidysks, session, now)
        if not retry:
            self._next_try[key] = float("inf")
        if follow is not None:
            self._follow_ups[key] = follow
        self._store.put_next_check(key, check)
        log.info(
            "next_episode_check",
            emby=emby.config.name,
            series=session.media.title,
            next=check.code,
            state=check.state.value,
            message=check.message,
        )

    async def _check(
        self,
        emby: EmbyIntegration,
        infinidysks: list[InfiniDyskIntegration],
        session: PlaybackSession,
        now: float,
    ) -> tuple[NextEpisodeCheck, bool, FollowUp | None]:
        """The outcome, whether to try again after ``RETRY_S``, and what to follow."""
        assert session.user_id and session.series_id and session.item_id
        try:
            nxt = await emby.next_episode(session.user_id, session.series_id, session.item_id)
        except IntegrationError as exc:
            return NextEpisodeCheck(state=NextCheckState.ERROR, message=exc.message), True, None
        if nxt is None:
            return NextEpisodeCheck(state=NextCheckState.NO_NEXT), False, None
        dav_id = dav_item_id(nxt.strm_id)
        if dav_id is None:
            return _result(nxt, NextCheckState.NO_FILE), True, None
        series = session.media.title
        asked = self._asked.get(dav_id)
        if asked is not None and now - asked[0] < RECENT_S:
            check = _result(nxt, NextCheckState.RECENT)
            return check, False, FollowUp(dav_id, asked[1], now + FOLLOW_S, check, series, True)

        outcomes: list[RecheckOutcome] = []
        errors: list[str] = []
        for infinidysk in infinidysks:
            try:
                outcomes.append(await infinidysk.recheck_file(dav_id))
            except IntegrationError as exc:
                errors.append(exc.message)
        accepted = next((o for o in outcomes if o.state is not None), None)
        if accepted is not None:
            since = self._wall()
            self._asked[dav_id] = (now, since)
            check = _result(nxt, _ACCEPTED.get(accepted.state or "", NextCheckState.QUEUED))
            return check, False, FollowUp(dav_id, since, now + FOLLOW_S, check, series)
        refused = next((o for o in outcomes if not o.not_found), None)
        if refused is not None:
            return _result(nxt, NextCheckState.REFUSED, refused.error), False, None
        if errors:
            return _result(nxt, NextCheckState.ERROR, errors[0]), True, None
        not_found = outcomes[0].error if outcomes else None
        return _result(nxt, NextCheckState.NOT_FOUND, not_found), False, None

    async def _follow(self, infinidysks: list[InfiniDyskIntegration], now: float) -> None:
        active: dict[UUID, ActiveCheck] = {}
        results: list[CheckResult] = []
        read = False
        for infinidysk in infinidysks:
            try:
                active.update(await infinidysk.active_checks())
                results.extend(await infinidysk.check_results())
            except IntegrationError as exc:
                log.debug(
                    "next_episode_follow_failed",
                    infinidysk=infinidysk.config.name,
                    error=exc.message,
                )
                continue
            read = True
        if not read:
            return  # nothing learned: try again next time
        for key, follow in list(self._follow_ups.items()):
            done = max(
                (
                    r
                    for r in results
                    if r.dav_item_id == follow.dav_id and r.at >= follow.since - CLOCK_SKEW
                ),
                key=lambda r: r.at,
                default=None,
            )
            running = active.get(follow.dav_id)
            if done is not None:
                state = finished_state(done)
                message = None if state is NextCheckState.HEALTHY else done.message
                check = follow.check.model_copy(
                    update={"state": state, "message": message, "progress": None}
                )
                del self._follow_ups[key]
                log.info(
                    "next_episode_result",
                    series=follow.series,
                    next=check.code,
                    state=state.value,
                    message=message,
                )
            elif running is not None:
                state = (
                    NextCheckState.REPAIRING
                    if running.phase == "Repairing"
                    else NextCheckState.CHECKING
                )
                check = follow.check.model_copy(
                    update={"state": state, "progress": running.progress}
                )
                follow.once = False
            elif follow.once:
                del self._follow_ups[key]  # requested earlier and not in recent history
                continue
            elif now >= follow.until:
                check = follow.check.model_copy(
                    update={"state": NextCheckState.NO_RESULT, "progress": None}
                )
                del self._follow_ups[key]
            else:
                continue
            follow.check = check
            self._store.put_next_check(key, check)


def _result(
    nxt: NextEpisode, state: NextCheckState, message: str | None = None
) -> NextEpisodeCheck:
    return NextEpisodeCheck(state=state, code=nxt.code, name=nxt.name, message=message)
