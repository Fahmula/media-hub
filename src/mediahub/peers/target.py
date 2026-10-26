"""Poll target for a linked peer: pulls its snapshot into ``SnapshotStore.peer()``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus
from mediahub.monitoring.demand import DemandTracker
from mediahub.monitoring.poller import IDLE_INTERVAL_S, PollTarget
from mediahub.monitoring.store import SnapshotStore
from mediahub.monitoring.targets import StatusCallback, StatusReporter
from mediahub.peers.client import PeerClient, insecure_transport
from mediahub.peers.repository import PeerConfig

log = get_logger(__name__)

ACTIVE_INTERVAL_S = 10.0
# (peer_id, remote server_id) -> None, after the first successful poll.
ServerIdCallback = Callable[[str, str], None]


def target_key(peer_id: str) -> str:
    return f"peer:{peer_id}"


class PeerTarget(PollTarget):
    def __init__(
        self,
        config: PeerConfig,
        store: SnapshotStore,
        demand: DemandTracker,
        *,
        on_status: StatusCallback | None = None,
        on_server_id: ServerIdCallback | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.key = target_key(config.id)
        self.client = PeerClient(config, store.identity.server_id)
        self._store = store
        self._demand = demand
        self._status = StatusReporter(config.id, on_status)
        self._on_server_id = on_server_id
        state = store.peer(config.id, config.name)
        state.insecure_transport = insecure_transport(config.base_url)

    # The poller's ``active`` includes demand from peers; following it would let two linked
    # servers keep each other busy forever, so only a local viewer counts here.
    def interval(self, active: bool) -> float:
        return ACTIVE_INTERVAL_S if self._demand.is_user_active() else IDLE_INTERVAL_S

    async def run_once(self, active: bool) -> bool:
        now = datetime.now(UTC)
        state = self._store.peer(self.config.id, self.config.name)
        try:
            snapshot = await self.client.fetch_snapshot(active=self._demand.is_user_active())
            other = self._store.peer_linked_as(snapshot.server_id, exclude=self.config.id)
            if other is not None:
                raise IntegrationError(
                    ErrorKind.PROTOCOL, f"This MediaHub is already linked as “{other.name}”"
                )
        except IntegrationError as exc:
            unreachable = exc.kind is ErrorKind.UNREACHABLE
            state.status = ServiceStatus.UNREACHABLE if unreachable else ServiceStatus.ERROR
            state.error = exc.message
            self._status.report(False, exc.message, now)
            return False
        except Exception:
            log.exception("peer_poll_failed", peer=self.config.name)
            state.status = ServiceStatus.ERROR
            state.error = "Unexpected error while reading the peer (see MediaHub logs)"
            self._status.report(False, "Unexpected error while reading the peer", now)
            return False

        self._store.put_peer(state, snapshot, now)
        self._status.report(True, None, now)
        if self.client.expected_server_id is None:
            self.client.expected_server_id = snapshot.server_id
            if self._on_server_id is not None:
                self._on_server_id(self.config.id, snapshot.server_id)
        return True

    async def aclose(self) -> None:
        await self.client.aclose()
