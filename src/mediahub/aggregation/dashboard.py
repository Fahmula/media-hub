"""Build view models for the selected scope (all servers, this server, or one peer).

Templates only ever see these view models and the normalized models inside them, never
raw third-party responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import ActivityEvent, DownloadItem, DownloadState
from mediahub.models.infinidysk import ActiveRead, InfiniDyskStats
from mediahub.models.issues import Issue, IssueCategory, Severity
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot
from mediahub.monitoring.store import PeerState, SnapshotStore

SCOPE_ALL = "all"
SCOPE_LOCAL = "local"

_DOWNLOAD_ORDER = {
    DownloadState.FAILED: 0,
    DownloadState.IMPORT_BLOCKED: 1,
    DownloadState.IMPORT_WARNING: 2,
    DownloadState.STALLED: 2,
    DownloadState.DOWNLOADING: 3,
    DownloadState.IMPORTING: 4,
    DownloadState.AWAITING_IMPORT: 5,
    DownloadState.WAITING: 6,
    DownloadState.UNKNOWN: 7,
}


@dataclass(frozen=True)
class ServerOption:
    key: str  # "local" or a peer id
    name: str
    is_local: bool
    status: ServiceStatus


@dataclass(frozen=True)
class ServiceSummary:
    source: SourceRef
    status: ServiceStatus
    headline: str
    count: int | None
    stale: bool
    error: str | None
    version: str | None
    checked_at: datetime | None


@dataclass(frozen=True)
class ServerCard:
    key: str
    server_id: str | None
    name: str
    is_local: bool
    status: ServiceStatus
    last_sync: datetime | None
    error: str | None
    services: list[ServiceSummary]
    insecure_transport: bool = False

    @property
    def services_up(self) -> int:
        return sum(1 for s in self.services if s.status.is_up and not s.stale)


@dataclass(frozen=True)
class StreamStats:
    source: SourceRef
    stats: InfiniDyskStats
    status: ServiceStatus
    stale: bool


@dataclass
class DashboardView:
    scope: str
    servers: list[ServerCard] = field(default_factory=list)
    snapshots: list[ServiceSnapshot] = field(default_factory=list)
    sessions: list[PlaybackSession] = field(default_factory=list)
    downloads: list[DownloadItem] = field(default_factory=list)
    history: list[ActivityEvent] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)  # what still needs attention
    hidden_issues: list[Issue] = field(default_factory=list)  # acknowledged or muted
    streams: list[StreamStats] = field(default_factory=list)
    # (session source key, session id) -> the InfiniDysk read serving that session's file
    stream_reads: dict[tuple[str, str], ActiveRead] = field(default_factory=dict)

    @property
    def multi_server(self) -> bool:
        return len(self.servers) > 1

    def read_for(self, session: PlaybackSession) -> ActiveRead | None:
        return self.stream_reads.get((session.source.key, session.session_id))

    @property
    def pending(self) -> bool:
        """No data has arrived yet for anything in scope (show skeletons)."""
        return bool(self.snapshots) and all(
            s.status is ServiceStatus.PENDING for s in self.snapshots
        )

    def snapshots_of(self, service_type: ServiceType) -> list[ServiceSnapshot]:
        return [s for s in self.snapshots if s.source.service_type is service_type]


def server_options(store: SnapshotStore) -> list[ServerOption]:
    options = [
        ServerOption(SCOPE_LOCAL, store.identity.server_name, True, ServiceStatus.OK),
    ]
    options += [ServerOption(p.peer_id, p.name, False, p.status) for p in store.peers()]
    return options


def resolve_scope(store: SnapshotStore, requested: str | None) -> str:
    if requested in (SCOPE_LOCAL, SCOPE_ALL):
        return requested
    if requested and any(p.peer_id == requested for p in store.peers()):
        return requested
    return SCOPE_ALL


def _headline(snap: ServiceSnapshot) -> tuple[str, int | None]:
    if snap.status is ServiceStatus.DISABLED:
        return "Disabled", None
    if snap.status is ServiceStatus.PENDING:
        return "Connecting…", None
    if not snap.status.is_up:
        return {"unreachable": "Offline", "error": "Error"}.get(snap.status.value, "Error"), None
    st = snap.source.service_type
    if st is ServiceType.EMBY:
        n = len(snap.sessions)
        return (f"{n} watching" if n else "Idle"), n
    if st in (ServiceType.SONARR, ServiceType.RADARR):
        active = sum(1 for d in snap.downloads if d.state is DownloadState.DOWNLOADING)
        if active:
            return f"{active} active", active
        if snap.downloads:
            return f"{len(snap.downloads)} queued", len(snap.downloads)
        return "Idle", 0
    if st is ServiceType.INFINIDYSK and snap.infinidysk is not None:
        n = snap.infinidysk.active_reads
        return (f"{n} streaming" if n else "Running"), n
    return "Running", None


def _summaries(snapshots: list[ServiceSnapshot]) -> list[ServiceSummary]:
    out = []
    for snap in snapshots:
        headline, count = _headline(snap)
        out.append(
            ServiceSummary(
                source=snap.source,
                status=snap.status,
                headline=headline,
                count=count,
                stale=snap.stale,
                error=snap.error,
                version=snap.version,
                checked_at=snap.checked_at,
            )
        )
    return out


def _peer_fingerprint(peer: PeerState, category: IssueCategory) -> str:
    return f"peer:{peer.peer_id}:{category}"


def _peer_issue(store: SnapshotStore, peer: PeerState) -> Issue | None:
    if peer.status is ServiceStatus.OK:
        store.issue_states.resolve(
            [
                _peer_fingerprint(peer, c)
                for c in (IssueCategory.PEER_DOWN, IssueCategory.PEER_INCOMPATIBLE)
            ],
            datetime.now(UTC),
        )
    if peer.status in (ServiceStatus.OK, ServiceStatus.PENDING, ServiceStatus.DISABLED):
        return None
    incompatible = peer.status is ServiceStatus.ERROR and bool(
        peer.error and "incompatible" in peer.error.lower()
    )
    category = IssueCategory.PEER_INCOMPATIBLE if incompatible else IssueCategory.PEER_DOWN
    return store.stamp_issue(
        Issue(
            fingerprint=_peer_fingerprint(peer, category),
            severity=Severity.ERROR,
            category=category,
            source=SourceRef(
                server_id=peer.snapshot.server_id if peer.snapshot else peer.peer_id,
                server_name=peer.name,
                instance_id=peer.peer_id,
                instance_name=peer.name,
                is_remote=True,
            ),
            title=f"{peer.name} is {'incompatible' if incompatible else 'not responding'}",
            detail=peer.error,
            link="/servers",
        )
    )


def _played_name(file_name: str) -> str:
    """Emby plays STRM files as ``<name>.strm``; unrenamed ones keep InfiniDysk's file name."""
    return file_name.lower().removesuffix(".strm")


def _last_segment(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1].lower()


def match_reads(snap: ServerSnapshot) -> dict[tuple[str, str], ActiveRead]:
    """Pair this server's Emby sessions with the InfiniDysk read of the same file.

    First by the STRM target (InfiniDysk's item id, the last segment of both the .strm URL and
    the read's ``path``), else by file name. Only live feeds count: without one there's no
    read list and no problem reports. An unmatched session gets no InfiniDysk health at all
    rather than a guess.
    """
    by_target: dict[str, ActiveRead] = {}
    by_name: dict[str, ActiveRead] = {}

    def keep(index: dict[str, ActiveRead], key: str, read: ActiveRead) -> None:
        if key not in index or (index[key].idle and not read.idle):
            index[key] = read

    for service in snap.services:
        stats = service.infinidysk
        if stats is None or not stats.live:
            continue
        for read in [*stats.reads, *stats.recent_reads]:
            keep(by_name, read.file_name.lower(), read)
            if read.path:
                keep(by_target, _last_segment(read.path), read)
    if not by_name:
        return {}
    out = {}
    for service in snap.services:
        for session in service.sessions:
            read = by_target.get(session.strm_target.lower()) if session.strm_target else None
            if read is None and session.file_name:
                read = by_name.get(_played_name(session.file_name))
            if read is not None:
                out[(session.source.key, session.session_id)] = read
    return out


def build_view(store: SnapshotStore, scope: str) -> DashboardView:
    view = DashboardView(scope=scope)
    servers: list[tuple[ServerCard, ServerSnapshot | None]] = []

    if scope in (SCOPE_ALL, SCOPE_LOCAL):
        local = store.local_server()
        servers.append(
            (
                ServerCard(
                    key=SCOPE_LOCAL,
                    server_id=local.server_id,
                    name=local.server_name,
                    is_local=True,
                    status=ServiceStatus.OK,
                    last_sync=local.generated_at,
                    error=None,
                    services=_summaries(local.services),
                ),
                local,
            )
        )
    for peer in store.peers():
        if scope not in (SCOPE_ALL, peer.peer_id):
            continue
        snap = store.remote_server(peer)
        servers.append(
            (
                ServerCard(
                    key=peer.peer_id,
                    server_id=snap.server_id if snap else None,
                    name=peer.name,
                    is_local=False,
                    status=peer.status,
                    last_sync=peer.last_success_at,
                    error=peer.error,
                    services=_summaries(snap.services) if snap else [],
                    insecure_transport=peer.insecure_transport,
                ),
                snap,
            )
        )
        issue = _peer_issue(store, peer)
        if issue is not None:
            view.issues.append(issue)

    for card, snap in servers:
        view.servers.append(card)
        if snap is None:
            continue
        view.stream_reads.update(match_reads(snap))
        for service in snap.services:
            view.snapshots.append(service)
            view.sessions.extend(service.sessions)
            view.downloads.extend(service.downloads)
            view.history.extend(service.history)
            view.issues.extend(service.issues)
            if service.infinidysk is not None:
                view.streams.append(
                    StreamStats(service.source, service.infinidysk, service.status, service.stale)
                )

    epoch = datetime.min.replace(tzinfo=UTC)
    view.sessions.sort(key=lambda s: (s.state.value != "playing", s.media.title.lower()))
    view.downloads.sort(key=lambda d: (_DOWNLOAD_ORDER.get(d.state, 9), -(d.progress_pct or 0)))
    view.history.sort(key=lambda e: e.at, reverse=True)
    now = datetime.now(UTC)
    issues = [store.issue_states.apply(i, now) for i in view.issues]
    issues.sort(key=lambda i: (i.severity.rank, -(i.first_seen or epoch).timestamp()))
    view.issues = [i for i in issues if not i.hidden]
    view.hidden_issues = [i for i in issues if i.hidden]
    return view
