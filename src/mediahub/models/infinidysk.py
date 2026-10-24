"""Normalized InfiniDysk streaming/cache statistics."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import computed_field

from mediahub.models.common import Model


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    UNKNOWN = "unknown"


class ProviderStats(Model):
    name: str
    circuit: CircuitState = CircuitState.UNKNOWN
    speed_mbps: float | None = None
    error_rate: float | None = None  # 0..1
    last_failure: str | None = None  # verbatim from InfiniDysk
    connections_active: int | None = None
    connections_max: int | None = None
    over_limit: bool = False  # reached its configured byte limit


class CacheStats(Model):
    enabled: bool = True
    used_bytes: int | None = None
    max_bytes: int | None = None
    entries: int | None = None
    hit_rate: float | None = None  # 0..1, computed from hit/miss counters

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_pct(self) -> float | None:
        if not self.max_bytes or self.used_bytes is None:
            return None
        return min(100.0, self.used_bytes / self.max_bytes * 100)


class ActiveRead(Model):
    read_id: str
    file_name: str
    path: str | None = None  # stripped before data leaves this server
    client_ip: str | None = None  # stripped before data leaves this server
    client_label: str | None = None  # e.g. "Emby" derived from the user agent
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    bytes_read: int | None = None
    file_size: int | None = None
    offset_pct: float | None = None
    speed_bps: float | None = None
    # InfiniDysk stopped tracking it (no data for 15 s); the player may still be playing
    # from its buffer. Kept for a while so buffered playback doesn't vanish from the list.
    idle: bool = False


class InfiniDyskStats(Model):
    healthy: bool | None = None
    ready: bool | None = None
    active_reads: int = 0
    articles_per_min: float | None = None
    errors_per_min: float | None = None
    bytes_served_per_min: float | None = None
    throughput: list[float] = []  # bytes/s buckets, oldest first, for sparklines
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    providers: list[ProviderStats] = []
    cache: CacheStats | None = None
    reads: list[ActiveRead] = []
    live: bool = False  # reads come from the live feed (backend WebSocket)
    feed_error: str | None = None  # why the live feed isn't connected, when configured
    queue_count: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def throughput_bps(self) -> float | None:
        return self.bytes_served_per_min / 60 if self.bytes_served_per_min is not None else None
