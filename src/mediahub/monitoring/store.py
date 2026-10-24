"""In-memory snapshot store: the only thing web routes read monitoring data from.

Writes come from the poller (one task per target); reads come from request handlers.
Everything runs on one event loop, so plain dicts are safe; snapshots are immutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from mediahub.db.app_settings import ServerIdentity
from mediahub.models.common import ServiceStatus
from mediahub.models.issues import Issue
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot
from mediahub.monitoring.issue_states import IssueStates
from mediahub.monitoring.issues import derive_issues

# A disconnected peer's last data stays visible (marked stale) for this long.
REMOTE_RETENTION_S = 600


@dataclass
class PeerState:
    peer_id: str
    name: str
    status: ServiceStatus = ServiceStatus.PENDING
    error: str | None = None
    last_success_at: datetime | None = None
    snapshot: ServerSnapshot | None = None
    insecure_transport: bool = False


@dataclass
class SnapshotStore:
    identity: ServerIdentity
    issue_states: IssueStates = field(default_factory=IssueStates)
    _local: dict[str, ServiceSnapshot] = field(default_factory=dict)
    _order: dict[str, int] = field(default_factory=dict)
    _peers: dict[str, PeerState] = field(default_factory=dict)
    _first_seen: dict[str, datetime] = field(default_factory=dict)
    _instance_fps: dict[str, set[str]] = field(default_factory=dict)

    # ----- local instances -------------------------------------------------------------
    def put_local(self, snapshot: ServiceSnapshot, order: int = 0) -> None:
        instance_id = snapshot.source.instance_id
        issues = [self._stamp(i) for i in derive_issues(snapshot)]
        current = {i.fingerprint for i in issues}
        for gone in self._instance_fps.get(instance_id, set()) - current:
            self._first_seen.pop(gone, None)  # resolved: a recurrence counts as new
        self._instance_fps[instance_id] = current
        now = datetime.now(UTC)
        if snapshot.status.is_up:
            self.issue_states.resolve_missing(snapshot.source.key, current, now)
        visible = [
            i
            for i in issues
            if not i.grace_s or (i.first_seen and (now - i.first_seen).total_seconds() >= i.grace_s)
        ]
        self._local[instance_id] = snapshot.model_copy(update={"issues": visible})
        self._order[instance_id] = order

    def get_local(self, instance_id: str) -> ServiceSnapshot | None:
        return self._local.get(instance_id)

    def remove_local(self, instance_id: str) -> None:
        removed = self._local.pop(instance_id, None)
        if removed is not None:
            self.issue_states.forget_source(removed.source.key)
        self._order.pop(instance_id, None)
        for fp in self._instance_fps.pop(instance_id, set()):
            self._first_seen.pop(fp, None)

    def local_snapshots(self) -> list[ServiceSnapshot]:
        return sorted(
            self._local.values(),
            key=lambda s: (self._order.get(s.source.instance_id, 0), s.source.instance_name),
        )

    def local_server(self) -> ServerSnapshot:
        return ServerSnapshot(
            server_id=self.identity.server_id,
            server_name=self.identity.server_name,
            is_local=True,
            generated_at=datetime.now(UTC),
            services=self.local_snapshots(),
        )

    def rename(self, identity: ServerIdentity) -> None:
        self.identity = identity

    # ----- remote peers ----------------------------------------------------------------
    def peer(self, peer_id: str, name: str) -> PeerState:
        state = self._peers.get(peer_id)
        if state is None:
            state = self._peers[peer_id] = PeerState(peer_id=peer_id, name=name)
        state.name = name
        return state

    def remove_peer(self, peer_id: str) -> None:
        self._peers.pop(peer_id, None)

    def peers(self) -> list[PeerState]:
        return sorted(self._peers.values(), key=lambda p: p.name.lower())

    def remote_server(self, state: PeerState) -> ServerSnapshot | None:
        snap = state.snapshot
        if snap is None:
            return None
        if state.last_success_at is not None:
            age = (datetime.now(UTC) - state.last_success_at).total_seconds()
            if state.status is not ServiceStatus.OK and age > REMOTE_RETENTION_S:
                return None
        return snap

    # ----- issues ----------------------------------------------------------------------
    def _stamp(self, issue: Issue) -> Issue:
        first = self._first_seen.get(issue.fingerprint)
        if first is None:
            # A restart loses the in-memory times; acted-on issues keep theirs in the DB.
            state = self.issue_states.get(issue.fingerprint)
            stored = state.first_seen if state else None
            first = issue.first_seen or stored or datetime.now(UTC)
            self._first_seen[issue.fingerprint] = first
        return issue.model_copy(update={"first_seen": first})

    def stamp_issue(self, issue: Issue) -> Issue:
        return self._stamp(issue)
