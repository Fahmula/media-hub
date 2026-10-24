"""Normalized download queue items and history events (Sonarr, Radarr, …)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import computed_field

from mediahub.models.common import Model, SourceRef


class DownloadState(StrEnum):
    """Dashboard state, derived from the *arr's own fields (see integrations/arr/mapper.py)."""

    DOWNLOADING = "downloading"
    WAITING = "waiting"
    STALLED = "stalled"  # client reported a warning (e.g. stalled torrent, missing files)
    AWAITING_IMPORT = "awaiting_import"
    IMPORTING = "importing"
    IMPORT_WARNING = "import_warning"  # import failed but the *arr keeps retrying
    IMPORT_BLOCKED = "import_blocked"  # needs manual action
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "downloading": "Downloading",
            "waiting": "Waiting",
            "stalled": "Warning",
            "awaiting_import": "Awaiting import",
            "importing": "Importing",
            "import_warning": "Import warning",
            "import_blocked": "Import blocked",
            "failed": "Failed",
        }.get(self.value, "Unknown")

    @property
    def needs_attention(self) -> bool:
        return self in (
            DownloadState.STALLED,
            DownloadState.IMPORT_WARNING,
            DownloadState.IMPORT_BLOCKED,
            DownloadState.FAILED,
        )

    @property
    def tone(self) -> str:
        """daisyUI colour name for badges/progress bars."""
        if self in (DownloadState.FAILED,):
            return "error"
        if self.needs_attention:
            return "warning"
        if self in (DownloadState.AWAITING_IMPORT, DownloadState.IMPORTING):
            return "success"
        if self is DownloadState.WAITING:
            return "neutral"
        return "primary"


class StatusMessage(Model):
    title: str | None = None
    messages: list[str] = []


class DownloadItem(Model):
    source: SourceRef
    item_id: str
    download_id: str | None = None
    title: str
    subtitle: str | None = None
    release_title: str | None = None
    image_url: str | None = None
    protocol: str | None = None
    client: str | None = None
    indexer: str | None = None
    quality: str | None = None
    size_bytes: int | None = None
    remaining_bytes: int | None = None
    eta_s: float | None = None
    added_at: datetime | None = None
    state: DownloadState = DownloadState.UNKNOWN
    raw_status: str | None = None
    raw_tracked_state: str | None = None
    raw_tracked_status: str | None = None
    messages: list[StatusMessage] = []
    error_message: str | None = None
    episode_count: int = 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> float | None:
        if not self.size_bytes or self.remaining_bytes is None:
            return None
        done = self.size_bytes - self.remaining_bytes
        return max(0.0, min(100.0, done / self.size_bytes * 100))


class ActivityKind(StrEnum):
    GRABBED = "grabbed"
    IMPORTED = "imported"
    FAILED = "failed"
    DELETED = "deleted"
    RENAMED = "renamed"
    IGNORED = "ignored"
    OTHER = "other"

    @property
    def label(self) -> str:
        return self.value.title()


class ActivityEvent(Model):
    source: SourceRef
    event_id: str
    kind: ActivityKind
    title: str
    subtitle: str | None = None
    image_url: str | None = None
    at: datetime
    detail: str | None = None
