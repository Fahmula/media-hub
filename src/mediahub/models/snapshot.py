"""Point-in-time snapshots of a service instance and of a whole server.

``ServerSnapshot`` is also the wire format for ``/peer/v1/snapshot`` (versioned).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import computed_field

from mediahub.models.common import Model, ServiceStatus, SourceRef
from mediahub.models.downloads import ActivityEvent, DownloadItem
from mediahub.models.infinidysk import InfiniDyskStats
from mediahub.models.issues import Issue, Severity
from mediahub.models.playback import PlaybackSession

SCHEMA_VERSION = 1


class HealthCheck(Model):
    """A health message reported by the service itself (e.g. Sonarr /api/v3/health)."""

    source: str | None = None
    level: Severity
    message: str
    wiki_url: str | None = None


class ServiceSnapshot(Model):
    source: SourceRef
    status: ServiceStatus = ServiceStatus.PENDING
    error: str | None = None
    version: str | None = None
    checked_at: datetime | None = None  # last poll attempt
    fetched_at: datetime | None = None  # last successful full data fetch
    poll_interval_s: float = 10
    health_checks: list[HealthCheck] = []
    sessions: list[PlaybackSession] = []
    downloads: list[DownloadItem] = []
    history: list[ActivityEvent] = []
    infinidysk: InfiniDyskStats | None = None
    import_warning_grace_s: int | None = None  # per-instance override of the default
    issues: list[Issue] = []

    def is_stale(self, now: datetime | None = None) -> bool:
        if self.fetched_at is None:
            return self.status is not ServiceStatus.PENDING
        now = now or datetime.now(UTC)
        return (now - self.fetched_at).total_seconds() > max(3 * self.poll_interval_s, 30)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stale(self) -> bool:
        return self.is_stale()


class ServerSnapshot(Model):
    schema_version: int = SCHEMA_VERSION
    server_id: str
    server_name: str
    is_local: bool = True
    generated_at: datetime
    services: list[ServiceSnapshot] = []
