"""Poll targets wrapping service integrations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from mediahub.integrations.base import Integration
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.poller import IDLE_INTERVAL_S, PollTarget
from mediahub.monitoring.store import SnapshotStore

log = get_logger(__name__)

# (instance_id, ok, error, at) -> None. Called only when the persisted status should change.
StatusCallback = Callable[[str, bool, str | None, datetime], None]

PERSIST_SUCCESS_EVERY_S = 300


class InstanceTarget(PollTarget):
    def __init__(
        self,
        integration: Integration,
        store: SnapshotStore,
        *,
        order: int = 0,
        on_status: StatusCallback | None = None,
    ) -> None:
        super().__init__()
        self.integration = integration
        self.key = integration.config.id
        self._store = store
        self._order = order
        self._on_status = on_status
        self._persisted: tuple[bool, str | None] | None = None
        self._persisted_at: datetime | None = None

    def interval(self, active: bool) -> float:
        return float(self.integration.interval_s) if active else IDLE_INTERVAL_S

    async def run_once(self, active: bool) -> bool:
        now = datetime.now(UTC)
        prev = self._store.get_local(self.key)
        base = prev or ServiceSnapshot(source=self.integration.source)
        interval = self.interval(active)
        try:
            result = await self.integration.poll(full=active)
        except IntegrationError as exc:
            status = (
                ServiceStatus.UNREACHABLE
                if exc.kind is ErrorKind.UNREACHABLE
                else ServiceStatus.ERROR
            )
            self._publish(
                base.model_copy(
                    update={
                        "status": status,
                        "error": exc.message,
                        "checked_at": now,
                        "poll_interval_s": interval,
                    }
                )
            )
            self._report(False, exc.message, now)
            return False
        except Exception:
            log.exception("integration_poll_failed", instance=self.integration.config.name)
            self._publish(
                base.model_copy(
                    update={
                        "status": ServiceStatus.ERROR,
                        "error": "Unexpected error while polling (see MediaHub logs)",
                        "checked_at": now,
                        "poll_interval_s": interval,
                    }
                )
            )
            self._report(False, "Unexpected error while polling", now)
            return False

        snapshot = ServiceSnapshot(
            source=self.integration.source,
            status=result.status,
            error=None,
            version=result.version or base.version,
            checked_at=now,
            fetched_at=now,
            poll_interval_s=interval,
            health_checks=result.health_checks,
            sessions=result.sessions if result.sessions is not None else base.sessions,
            downloads=result.downloads if result.downloads is not None else base.downloads,
            history=result.history if result.history is not None else base.history,
            infinidysk=result.infinidysk or base.infinidysk,
            import_warning_grace_s=result.import_warning_grace_s,
        )
        self._publish(snapshot)
        self._report(True, None, now)
        return True

    def _publish(self, snapshot: ServiceSnapshot) -> None:
        self._store.put_local(snapshot, order=self._order)

    def _report(self, ok: bool, error: str | None, at: datetime) -> None:
        if self._on_status is None:
            return
        state = (ok, error)
        due = (
            ok
            and self._persisted_at is not None
            and (at - self._persisted_at).total_seconds() >= PERSIST_SUCCESS_EVERY_S
        )
        if state != self._persisted or due:
            self._persisted, self._persisted_at = state, at
            self._on_status(self.key, ok, error, at)

    async def aclose(self) -> None:
        await self.integration.aclose()
