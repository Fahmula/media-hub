"""Problems that need attention, aggregated across services and servers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from mediahub.models.common import Model, SourceRef


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]


class IssueCategory(StrEnum):
    UNREACHABLE = "unreachable"
    AUTH = "auth"
    PROTOCOL = "protocol"
    HEALTH_CHECK = "health_check"
    DOWNLOAD_STALLED = "download_stalled"
    DOWNLOAD_FAILED = "download_failed"
    IMPORT_BLOCKED = "import_blocked"
    STREAM_ERROR = "stream_error"
    PROVIDER = "provider"
    PEER_DOWN = "peer_down"
    PEER_INCOMPATIBLE = "peer_incompatible"

    @property
    def label(self) -> str:
        return {
            "unreachable": "Unreachable",
            "auth": "Authentication",
            "protocol": "Unexpected response",
            "health_check": "Health check",
            "download_stalled": "Download warning",
            "download_failed": "Download failed",
            "import_blocked": "Import blocked",
            "stream_error": "Streaming",
            "provider": "Provider",
            "peer_down": "Peer offline",
            "peer_incompatible": "Peer incompatible",
        }[self.value]


class Issue(Model):
    fingerprint: str
    severity: Severity
    category: IssueCategory
    source: SourceRef
    title: str
    detail: str | None = None  # verbatim upstream message where available
    first_seen: datetime | None = None
    link: str | None = None
