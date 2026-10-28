"""The contract every service integration implements.

An integration owns one ``httpx.AsyncClient`` and turns the service's API into normalized
models (``mediahub.models``). Integrations never touch the database or templates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import ActivityEvent, DownloadItem
from mediahub.models.infinidysk import InfiniDyskStats
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import HealthCheck


class Capability(StrEnum):
    PLAYBACK = "playback"
    DOWNLOADS = "downloads"
    HISTORY = "history"
    STREAM_STATS = "stream_stats"
    CONTROL = "control"


@dataclass(frozen=True)
class InstanceConfig:
    """Decrypted instance configuration handed to an integration. Never rendered."""

    id: str
    service_type: ServiceType
    name: str
    base_url: str
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    options: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    poll_interval_s: int | None = None


@dataclass
class CheckStep:
    label: str
    ok: bool
    detail: str | None = None


@dataclass
class ConnectionTestResult:
    ok: bool
    steps: list[CheckStep] = field(default_factory=list)
    version: str | None = None
    server_name: str | None = None

    def add(self, label: str, ok: bool, detail: str | None = None) -> bool:
        self.steps.append(CheckStep(label, ok, detail))
        return ok


@dataclass
class PollResult:
    """What an integration reports from one poll; the poller wraps it in a ServiceSnapshot."""

    status: ServiceStatus = ServiceStatus.OK
    version: str | None = None
    health_checks: list[HealthCheck] = field(default_factory=list)
    sessions: list[PlaybackSession] | None = None  # None = not fetched this round
    downloads: list[DownloadItem] | None = None
    history: list[ActivityEvent] | None = None
    infinidysk: InfiniDyskStats | None = None
    import_warning_grace_s: int | None = None  # None = the default


@dataclass(frozen=True)
class FieldSpec:
    """Describes an extra form field an integration needs (rendered by the settings UI)."""

    name: str
    label: str
    secret: bool = False
    required: bool = False
    placeholder: str = ""
    help: str = ""
    kind: str = "text"  # text | url | checkbox | minutes | percent


class Integration(ABC):
    service_type: ClassVar[ServiceType]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    default_interval_s: ClassVar[int] = 10
    default_url: ClassVar[str] = ""
    # Secrets every integration of this type requires; extra fields are declared per type.
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("api_key", "API key", secret=True, required=True),
    )
    option_fields: ClassVar[tuple[FieldSpec, ...]] = ()

    def __init__(self, config: InstanceConfig, source: SourceRef) -> None:
        self.config = config
        self.source = source

    @property
    def interval_s(self) -> int:
        return self.config.poll_interval_s or self.default_interval_s

    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult: ...

    @abstractmethod
    async def poll(self, *, full: bool) -> PollResult:
        """Fetch current state. ``full=False`` is the cheap idle pass (health only)."""

    async def fetch_image(self, ref: str) -> tuple[bytes, str] | None:
        """Return (bytes, content-type) for an opaque image reference, if supported."""
        return None

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass
