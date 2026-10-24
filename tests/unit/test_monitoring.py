from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mediahub.aggregation.dashboard import SCOPE_ALL, SCOPE_LOCAL, build_view, resolve_scope
from mediahub.db.app_settings import ServerIdentity
from mediahub.integrations.base import (
    ConnectionTestResult,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.demo import DemoPeerTarget
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import DownloadItem, DownloadState, StatusMessage
from mediahub.models.issues import IssueCategory, Severity
from mediahub.models.snapshot import HealthCheck, ServiceSnapshot
from mediahub.monitoring.demand import DemandTracker
from mediahub.monitoring.poller import Poller, backoff_delay
from mediahub.monitoring.store import SnapshotStore
from mediahub.monitoring.targets import InstanceTarget

IDENTITY = ServerIdentity(server_id="srv-1", server_name="Skynet")


def source(instance: str = "i1", st: ServiceType = ServiceType.SONARR) -> SourceRef:
    return SourceRef(
        server_id="srv-1",
        server_name="Skynet",
        instance_id=instance,
        instance_name="TV",
        service_type=st,
    )


class FakeIntegration(Integration):
    service_type = ServiceType.SONARR

    def __init__(self, results: list[PollResult | Exception], delay: float = 0) -> None:
        config = InstanceConfig(
            id="i1", service_type=ServiceType.SONARR, name="TV", base_url="http://x"
        )
        super().__init__(config, source())
        self.results = results
        self.delay = delay
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(ok=True)

    async def poll(self, *, full: bool) -> PollResult:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            result = self.results[min(self.calls - 1, len(self.results) - 1)]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.concurrent -= 1


async def test_target_success_then_failure_keeps_last_data() -> None:
    store = SnapshotStore(identity=IDENTITY)
    item = DownloadItem(source=source(), item_id="1", title="Show", state=DownloadState.DOWNLOADING)
    integ = FakeIntegration(
        [
            PollResult(version="4.0", downloads=[item]),
            IntegrationError(ErrorKind.UNREACHABLE, "Connection refused"),
        ]
    )
    statuses: list[tuple[bool, str | None]] = []
    target = InstanceTarget(
        integ, store, on_status=lambda _i, ok, err, _at: statuses.append((ok, err))
    )

    assert await target.run_once(True)
    snap = store.get_local("i1")
    assert snap is not None and snap.status is ServiceStatus.OK and snap.version == "4.0"

    assert not await target.run_once(True)
    snap = store.get_local("i1")
    assert snap is not None
    assert snap.status is ServiceStatus.UNREACHABLE
    assert snap.error == "Connection refused"
    assert len(snap.downloads) == 1  # last good data kept, flagged by fetched_at age
    assert [i.category for i in snap.issues] == [IssueCategory.UNREACHABLE]
    assert statuses == [(True, None), (False, "Connection refused")]

    # Repeated identical failure isn't persisted again.
    await target.run_once(True)
    assert len(statuses) == 2


async def test_unexpected_exception_is_contained() -> None:
    store = SnapshotStore(identity=IDENTITY)
    target = InstanceTarget(FakeIntegration([RuntimeError("boom")]), store)
    assert not await target.run_once(True)
    snap = store.get_local("i1")
    assert snap is not None and snap.status is ServiceStatus.ERROR
    assert "boom" not in (snap.error or "")


async def test_poller_never_overlaps_a_target() -> None:
    store = SnapshotStore(identity=IDENTITY)
    integ = FakeIntegration([PollResult()], delay=0.05)
    demand = DemandTracker()
    poller = Poller(demand)
    target = InstanceTarget(integ, store)
    await poller.add(target)
    await asyncio.gather(*(poller.poll_now("i1") for _ in range(5)))
    await poller.stop()
    assert integ.max_concurrent == 1
    assert integ.calls >= 5


async def test_demand_wakes_idle_pollers() -> None:
    store = SnapshotStore(identity=IDENTITY)
    integ = FakeIntegration([PollResult()])
    demand = DemandTracker()
    demand.bind_loop(asyncio.get_running_loop())
    poller = Poller(demand)
    await poller.add(InstanceTarget(integ, store))
    await asyncio.sleep(0.05)
    assert integ.calls == 1  # first poll, then sleeping for the 60 s idle interval
    demand.touch()
    await asyncio.sleep(0.05)
    assert integ.calls == 2
    await poller.stop()


def test_backoff() -> None:
    assert backoff_delay(10, 0) == 10
    assert backoff_delay(10, 1) == 10
    assert backoff_delay(10, 3) == 40
    assert backoff_delay(10, 20) == 300
    assert backoff_delay(1, 1) == 5


def test_stale_detection() -> None:
    now = datetime.now(UTC)
    fresh = ServiceSnapshot(
        source=source(), status=ServiceStatus.OK, fetched_at=now, poll_interval_s=10
    )
    old = fresh.model_copy(update={"fetched_at": now - timedelta(seconds=45)})
    assert not fresh.is_stale(now)
    assert old.is_stale(now)
    assert not ServiceSnapshot(source=source()).is_stale(now)  # pending isn't "stale"


def test_issues_from_downloads_and_health_use_upstream_text() -> None:
    store = SnapshotStore(identity=IDENTITY)
    blocked = DownloadItem(
        source=source(),
        item_id="1",
        title="House of the Dragon",
        state=DownloadState.IMPORT_BLOCKED,
        messages=[StatusMessage(title="file.mkv", messages=["Not an upgrade"])],
    )
    stalled = DownloadItem(
        source=source(),
        item_id="2",
        title="Civil War",
        state=DownloadState.STALLED,
        error_message="The download is stalled with no connections",
    )
    ok = DownloadItem(source=source(), item_id="3", title="Fine", state=DownloadState.DOWNLOADING)
    store.put_local(
        ServiceSnapshot(
            source=source(),
            status=ServiceStatus.OK,
            downloads=[blocked, stalled, ok],
            health_checks=[
                HealthCheck(level=Severity.WARNING, message="Indexers unavailable"),
                HealthCheck(level=Severity.INFO, message="Update available"),
            ],
        )
    )
    snap = store.get_local("i1")
    assert snap is not None
    by_cat = {i.category: i for i in snap.issues}
    assert by_cat[IssueCategory.IMPORT_BLOCKED].detail == "file.mkv; Not an upgrade"
    assert by_cat[IssueCategory.DOWNLOAD_STALLED].detail == (
        "The download is stalled with no connections"
    )
    assert by_cat[IssueCategory.HEALTH_CHECK].title == "Indexers unavailable"
    assert len(snap.issues) == 3
    first_seen = by_cat[IssueCategory.HEALTH_CHECK].first_seen

    # first_seen is stable across polls while the issue persists.
    store.put_local(snap.model_copy(update={"issues": []}))
    again = store.get_local("i1")
    assert again is not None
    assert {i.first_seen for i in again.issues if i.category is IssueCategory.HEALTH_CHECK} == {
        first_seen
    }


async def test_scopes_and_demo_peer_redaction() -> None:
    store = SnapshotStore(identity=IDENTITY)
    store.put_local(ServiceSnapshot(source=source(), status=ServiceStatus.OK))
    await DemoPeerTarget(store).run_once(True)
    peer_id = store.peers()[0].peer_id

    all_view = build_view(store, SCOPE_ALL)
    assert len(all_view.servers) == 2 and all_view.multi_server
    local_view = build_view(store, SCOPE_LOCAL)
    assert [s.name for s in local_view.servers] == ["Skynet"]
    peer_view = build_view(store, peer_id)
    assert [s.name for s in peer_view.servers] == ["Brother Server"]
    assert peer_view.sessions and all(s.source.is_remote for s in peer_view.sessions)
    assert all(s.remote_address is None for s in peer_view.sessions)
    reads = [r for st in peer_view.streams for r in st.stats.reads]
    assert reads and all(r.path is None and r.client_ip is None for r in reads)

    assert resolve_scope(store, "nope") == SCOPE_ALL
    assert resolve_scope(store, peer_id) == peer_id


async def test_down_peer_raises_issue() -> None:
    store = SnapshotStore(identity=IDENTITY)
    state = store.peer("p1", "Brother")
    state.status = ServiceStatus.UNREACHABLE
    state.error = "Connection refused"
    view = build_view(store, SCOPE_ALL)
    assert [i.category for i in view.issues] == [IssueCategory.PEER_DOWN]


@pytest.mark.parametrize("bad", ["", "emby:8096", "ftp://x", "http://u:p@host", "http://h/?a=1"])
def test_url_validation_rejects(bad: str) -> None:
    from mediahub.instances.urls import InvalidURLError, normalize_base_url

    with pytest.raises(InvalidURLError):
        normalize_base_url(bad)


def test_url_normalization() -> None:
    from mediahub.instances.urls import normalize_base_url

    assert normalize_base_url(" HTTP://Emby.LAN:8096/ ") == "http://emby.lan:8096"
    assert normalize_base_url("https://h/sonarr/") == "https://h/sonarr"
    assert normalize_base_url("http://[::1]:8989") == "http://[::1]:8989"


def test_import_warning_issue_waits_for_grace_period() -> None:
    store = SnapshotStore(identity=IDENTITY)
    warning = DownloadItem(
        source=source(),
        item_id="1",
        title="Supergirl",
        state=DownloadState.IMPORT_WARNING,
        messages=[StatusMessage(title="r", messages=["No files found are eligible for import"])],
    )
    blocked = warning.model_copy(update={"item_id": "2", "state": DownloadState.IMPORT_BLOCKED})
    snap = ServiceSnapshot(source=source(), status=ServiceStatus.OK, downloads=[warning, blocked])

    store.put_local(snap)
    first = store.get_local("i1")
    assert first is not None
    assert [i.link for i in first.issues] == ["/sonarr?instance=i1#dl-2"]  # blocked: at once

    # Still there after the grace period: now it is an issue too.
    for fp in store._first_seen:
        store._first_seen[fp] -= timedelta(seconds=601)
    store.put_local(snap)
    later = store.get_local("i1")
    assert later is not None and len(later.issues) == 2

    # Resolved, then back: the clock starts again.
    store.put_local(snap.model_copy(update={"downloads": []}))
    store.put_local(snap)
    again = store.get_local("i1")
    assert again is not None and len(again.issues) == 1


def test_import_warning_delay_is_configurable_per_instance() -> None:
    store = SnapshotStore(identity=IDENTITY)
    warning = DownloadItem(
        source=source(), item_id="1", title="X", state=DownloadState.IMPORT_WARNING
    )
    snap = ServiceSnapshot(source=source(), downloads=[warning], import_warning_grace_s=0)
    store.put_local(snap)
    immediate = store.get_local("i1")
    assert immediate is not None and len(immediate.issues) == 1

    store.put_local(snap.model_copy(update={"downloads": [], "import_warning_grace_s": None}))
    store.put_local(snap.model_copy(update={"import_warning_grace_s": 3600}))
    for fp in store._first_seen:
        store._first_seen[fp] -= timedelta(seconds=601)  # past the default, not the override
    store.put_local(snap.model_copy(update={"import_warning_grace_s": 3600}))
    delayed = store.get_local("i1")
    assert delayed is not None and delayed.issues == []
