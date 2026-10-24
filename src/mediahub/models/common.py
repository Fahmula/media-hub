"""Shared building blocks for normalized monitoring data."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ServiceType(StrEnum):
    EMBY = "emby"
    SONARR = "sonarr"
    RADARR = "radarr"
    INFINIDYSK = "infinidysk"

    @property
    def label(self) -> str:
        return {"emby": "Emby", "sonarr": "Sonarr", "radarr": "Radarr"}.get(
            self.value, "InfiniDysk"
        )


class ServiceStatus(StrEnum):
    PENDING = "pending"  # no poll has completed yet
    OK = "ok"
    DEGRADED = "degraded"  # reachable, but reporting problems
    ERROR = "error"  # reachable, but a request failed (auth, protocol)
    UNREACHABLE = "unreachable"
    DISABLED = "disabled"

    @property
    def is_up(self) -> bool:
        return self in (ServiceStatus.OK, ServiceStatus.DEGRADED)


class Model(BaseModel):
    """Base for all normalized models: immutable, tolerant of unknown fields from peers."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class SourceRef(Model):
    """Where an item came from. Every normalized item carries one."""

    server_id: str
    server_name: str
    instance_id: str
    instance_name: str
    service_type: ServiceType | None = None  # None = the server itself (e.g. a peer link)
    is_remote: bool = False

    @property
    def key(self) -> str:
        return f"{self.server_id}:{self.instance_id}"

    @property
    def slug(self) -> str:
        """URL/icon slug: the service type, or "peer" for server-level sources."""
        return self.service_type.value if self.service_type else "peer"
