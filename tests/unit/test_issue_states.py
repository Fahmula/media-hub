"""Failed downloads from history, and acknowledging / muting issues."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mediahub.aggregation.dashboard import SCOPE_ALL, build_view
from mediahub.db.app_settings import ServerIdentity
from mediahub.db.models import IssueStateRow
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.integrations.arr.mapper import map_history, map_queue
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import ActivityEvent, ActivityKind, DownloadItem
from mediahub.models.issues import Issue, IssueCategory, Severity
from mediahub.models.snapshot import HealthCheck, ServiceSnapshot
from mediahub.monitoring.issue_states import IssueStates
from mediahub.monitoring.issues import FAILED_RETRY_GRACE_S, derive_issues
from mediahub.monitoring.store import SnapshotStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
IDENTITY = ServerIdentity(server_id="srv-1", server_name="Skynet")
SRC = SourceRef(
    server_id="srv-1",
    server_name="Skynet",
    instance_id="i1",
    instance_name="Movies",
    service_type=ServiceType.RADARR,
)
NOW = datetime.now(UTC)


def event(
    kind: ActivityKind, minutes_ago: float, media: list[str], event_id: str = "1"
) -> ActivityEvent:
    return ActivityEvent(
        source=SRC,
        event_id=event_id,
        download_id=f"dl-{event_id}",
        kind=kind,
        title="Movie 1",
        subtitle="2026",
        at=NOW - timedelta(minutes=minutes_ago),
        detail="Aborted, cannot be completed" if kind is ActivityKind.FAILED else None,
        media_ids=media,
    )


def failures(
    history: list[ActivityEvent], downloads: list[DownloadItem] | None = None
) -> list[Issue]:
    snap = ServiceSnapshot(
        source=SRC, status=ServiceStatus.OK, history=history, downloads=downloads or []
    )
    return [i for i in derive_issues(snap) if i.category is IssueCategory.DOWNLOAD_FAILED]


# ----- failed downloads from history ---------------------------------------------------------
def test_failure_without_retry_is_an_issue_after_grace() -> None:
    [issue] = failures([event(ActivityKind.FAILED, 30, ["m1"])])
    assert issue.title == "Movie 1 · 2026"
    assert issue.detail == "Aborted, cannot be completed"  # verbatim upstream message
    assert issue.severity is Severity.WARNING
    assert issue.first_seen == NOW - timedelta(minutes=30)  # when it failed, not when seen
    assert issue.grace_s == FAILED_RETRY_GRACE_S


def test_failure_superseded_by_later_grab_import_or_queue() -> None:
    failed = event(ActivityKind.FAILED, 30, ["m1"], "1")
    assert failures([event(ActivityKind.GRABBED, 29, ["m1"], "2"), failed]) == []
    assert failures([event(ActivityKind.IMPORTED, 29, ["m1"], "2"), failed]) == []
    queued = DownloadItem(source=SRC, item_id="q", title="Movie 1", media_ids=["m1"])
    assert failures([failed], [queued]) == []
    # The grab of the failed download itself came before the failure: not a retry.
    assert len(failures([failed, event(ActivityKind.GRABBED, 40, ["m1"], "0")])) == 1
    # Another movie's grab doesn't count.
    assert len(failures([event(ActivityKind.GRABBED, 29, ["m2"], "2"), failed])) == 1


def test_season_pack_failure_needs_every_episode_retried() -> None:
    failed = event(ActivityKind.FAILED, 30, ["e1", "e2"], "1")
    assert len(failures([event(ActivityKind.GRABBED, 20, ["e1"], "2"), failed])) == 1
    both = [event(ActivityKind.GRABBED, 20, ["e1"], "2"), event(ActivityKind.GRABBED, 10, ["e2"])]
    assert failures([*both, failed]) == []


def test_old_failures_are_ignored() -> None:
    assert failures([event(ActivityKind.FAILED, 8 * 24 * 60, ["m1"])]) == []


def test_live_radarr_capture_retried_failures_raise_nothing() -> None:
    # Two SABnzbd failures, each followed by a re-grab; the third release imported.
    data = json.loads((FIXTURES / "radarr" / "history.json").read_text())
    history = map_history(data["history"]["records"], SRC, lambda *_: None)
    assert sum(e.kind is ActivityKind.FAILED for e in history) == 2
    assert all(e.media_ids for e in history)
    assert failures(history) == []


@pytest.mark.parametrize(("minutes_ago", "shown"), [(2, False), (20, True)])
def test_failure_grace_runs_from_the_failure(minutes_ago: float, shown: bool) -> None:
    store = SnapshotStore(identity=IDENTITY)
    history = [event(ActivityKind.FAILED, minutes_ago, ["m1"])]
    store.put_local(ServiceSnapshot(source=SRC, status=ServiceStatus.OK, history=history))
    snap = store.get_local("i1")
    assert snap is not None and len(snap.issues) == int(shown)


# ----- acknowledge / mute --------------------------------------------------------------------
@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_migrations(engine)
    return make_sessionmaker(engine)


def health_snapshot(*messages: str, status: ServiceStatus = ServiceStatus.OK) -> ServiceSnapshot:
    return ServiceSnapshot(
        source=SRC,
        status=status,
        health_checks=[HealthCheck(level=Severity.WARNING, message=m) for m in messages],
    )


def rows(sessions: sessionmaker[Session]) -> dict[str, IssueStateRow]:
    with session_scope(sessions) as db:
        return {r.fingerprint: r for r in db.scalars(select(IssueStateRow))}


async def test_acknowledge_hides_until_resolved(sessions: sessionmaker[Session]) -> None:
    states = IssueStates(sessions)
    store = SnapshotStore(identity=IDENTITY, issue_states=states)
    store.put_local(health_snapshot("Indexers unavailable", "Disk low"))
    view = build_view(store, SCOPE_ALL)
    indexers = next(i for i in view.issues if i.title == "Indexers unavailable")

    await states.acknowledge(indexers)
    view = build_view(store, SCOPE_ALL)
    assert [i.title for i in view.issues] == ["Disk low"]
    assert [i.title for i in view.hidden_issues] == ["Indexers unavailable"]
    assert view.hidden_issues[0].acknowledged_at is not None
    assert indexers.fingerprint in rows(sessions)

    # Kept across a restart, including when it was first seen.
    reloaded = IssueStates(sessions)
    reloaded.load()
    restarted = SnapshotStore(identity=IDENTITY, issue_states=reloaded)
    restarted.put_local(health_snapshot("Indexers unavailable"))
    view = build_view(restarted, SCOPE_ALL)
    assert view.issues == [] and view.hidden_issues[0].first_seen == indexers.first_seen

    # A failed poll keeps the last data: not a resolution.
    restarted.put_local(health_snapshot(status=ServiceStatus.UNREACHABLE))
    assert reloaded.get(indexers.fingerprint) is not None

    # Gone from a successful poll: resolved. If it recurs, it needs attention again.
    restarted.put_local(health_snapshot())
    await reloaded.aclose()
    assert reloaded.get(indexers.fingerprint) is None
    assert rows(sessions) == {}
    restarted.put_local(health_snapshot("Indexers unavailable"))
    assert [i.title for i in build_view(restarted, SCOPE_ALL).issues] == ["Indexers unavailable"]


async def test_mute_outlives_resolution_until_it_expires(sessions: sessionmaker[Session]) -> None:
    states = IssueStates(sessions)
    store = SnapshotStore(identity=IDENTITY, issue_states=states)
    store.put_local(health_snapshot("Flapping"))
    [issue] = build_view(store, SCOPE_ALL).issues

    await states.mute(issue, NOW + timedelta(hours=1))
    store.put_local(health_snapshot())  # resolved
    store.put_local(health_snapshot("Flapping"))  # and back, still muted
    view = build_view(store, SCOPE_ALL)
    assert view.issues == [] and view.hidden_issues[0].muted_until is not None

    # Expired: shown again, and the stored mute is tidied away on the next poll.
    state = states.get(issue.fingerprint)
    assert state is not None
    states._states[issue.fingerprint] = replace(state, muted_until=NOW - timedelta(seconds=1))
    assert len(build_view(store, SCOPE_ALL).issues) == 1
    store.put_local(health_snapshot("Flapping"))
    await states.aclose()
    assert states.get(issue.fingerprint) is None and rows(sessions) == {}


async def test_restore_and_removed_instance(sessions: sessionmaker[Session]) -> None:
    states = IssueStates(sessions)
    store = SnapshotStore(identity=IDENTITY, issue_states=states)
    store.put_local(health_snapshot("A", "B"))
    a, b = build_view(store, SCOPE_ALL).issues
    await states.acknowledge(a)
    await states.mute(b, NOW + timedelta(days=1))

    await states.restore(a.fingerprint)
    assert [i.fingerprint for i in build_view(store, SCOPE_ALL).issues] == [a.fingerprint]

    store.remove_local("i1")
    await states.aclose()
    assert states.get(b.fingerprint) is None and rows(sessions) == {}


async def test_peer_issue_ack_clears_when_peer_recovers() -> None:
    store = SnapshotStore(identity=IDENTITY)
    peer = store.peer("p1", "Brother")
    peer.status, peer.error = ServiceStatus.UNREACHABLE, "Connection refused"
    [issue] = build_view(store, SCOPE_ALL).issues
    await store.issue_states.acknowledge(issue)
    assert build_view(store, SCOPE_ALL).issues == []

    peer.status = ServiceStatus.OK
    build_view(store, SCOPE_ALL)
    peer.status = ServiceStatus.UNREACHABLE
    assert [i.fingerprint for i in build_view(store, SCOPE_ALL).issues] == [issue.fingerprint]


def test_download_items_carry_media_ids() -> None:
    data = json.loads((FIXTURES / "sonarr" / "constructed.json").read_text())
    items = map_queue(data["queue"]["records"], SRC, lambda *_: None)
    pack = next(d for d in items if d.episode_count > 1)
    assert len(pack.media_ids) == pack.episode_count
    assert all(m.startswith("e") for m in pack.media_ids)
