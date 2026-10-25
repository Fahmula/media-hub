"""Map InfiniDysk responses to :class:`InfiniDyskStats`.

Wire format taken from the InfiniDysk source (``main`` @ a72e11d7, v1.6.x):

* ``/api/get-provider-usage``: ``providers[]`` with ``host``, ``nickname``, ``providerId``
  (the provider's *metrics key*, also used as ``provider_key`` in ``/metrics`` and as
  ``provider`` in overview rows and live breakers), ``overLimit``, ``effectiveMaxConnections``
  and ``connectionBudget.activeTransferOperations``.
* ``/api/get-overview-stats``: ``tiles``, ``throughput[]`` (per-bucket ``bytesServed``,
  bucket size ``throughputBucketSizeMs``), ``providers[]`` (``speedMbPerSec``, ``errorRate``
  0..1, ``circuitState`` ``closed|open|halfOpen``, ``lastFailureReason``), ``latency``,
  ``failover`` (``articlesRecovered``: articles another provider served after a miss in the
  window; ``rescuedBy[]`` ``{provider, nickname, saves}``).
* ``/metrics``: ``nzbdav_circuit_state{provider_key}`` (0 closed, 1 open, 2 half-open),
  ``nzbdav_nntp_pool_connections{provider_key,state="active"}``,
  ``nzbdav_nntp_pool_max_connections{provider_key,limit="effective"}``, ``nzbdav_active_reads``
  and the ``nzbdav_segment_cache_*`` series.
* WebSocket topics ``ls`` (live tiles + ``providerBreakers``, every 5 s) and ``ar``
  (``{reads: [...]}``, sent only when something changed). Times are unix milliseconds.

Everything is optional: fields missing from a version are simply left empty.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mediahub.integrations.infinidysk.metrics import Metrics
from mediahub.models.infinidysk import (
    ActiveRead,
    CacheStats,
    CircuitState,
    InfiniDyskStats,
    ProviderStats,
    ReadProblem,
)

_CIRCUIT_WIRE = {
    "closed": CircuitState.CLOSED,
    "open": CircuitState.OPEN,
    "halfOpen": CircuitState.HALF_OPEN,
}
_CIRCUIT_METRIC = {0: CircuitState.CLOSED, 1: CircuitState.OPEN, 2: CircuitState.HALF_OPEN}
# User-agent substring (lowercase) -> label. Checked in order.
_CLIENTS = (
    ("emby", "Emby"),
    ("jellyfin", "Jellyfin"),
    ("plex", "Plex"),
    ("lavf", "FFmpeg"),
    ("kodi", "Kodi"),
    ("vlc", "VLC"),
    ("rclone", "rclone"),
    ("infuse", "Infuse"),
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _ms(value: Any) -> datetime | None:
    number = _num(value)
    if number is None or number <= 0:
        return None
    return datetime.fromtimestamp(number / 1000, UTC)


def client_label(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    lowered = user_agent.lower()
    for needle, label in _CLIENTS:
        if needle in lowered:
            return label
    return user_agent.split("/")[0].split(" ")[0][:32] or None


def map_read(
    raw: Mapping[str, Any],
    speed_bps: float | None = None,
    *,
    idle: bool = False,
    problems: list[ReadProblem] | None = None,
) -> ActiveRead:
    size = _int(raw.get("fileSize"))
    served = [
        (name, _int(p.get("segments")) or 0)
        for p in map(_dict, _list(raw.get("providers")))
        if (name := _str(p.get("nickname")) or _str(p.get("host")))
    ]
    offset = _num(raw.get("currentOffset"))
    return ActiveRead(
        read_id=str(raw.get("id", "")),
        file_name=_str(raw.get("fileName")) or "Unknown file",
        path=_str(raw.get("path")),
        client_ip=_str(raw.get("clientIp")),
        client_label=client_label(_str(raw.get("clientUserAgent"))),
        started_at=_ms(raw.get("startedAt")),
        last_activity_at=_ms(raw.get("lastActivityAt")),
        bytes_read=_int(raw.get("bytesRead")),
        file_size=size if size and size > 0 else None,
        offset_pct=min(100.0, offset / size * 100) if size and offset is not None else None,
        speed_bps=speed_bps,
        idle=idle,
        providers=[name for name, _ in sorted(served, key=lambda s: -s[1])],
        problems=problems or [],
    )


def map_cache(metrics: Metrics) -> CacheStats | None:
    enabled = metrics.value("nzbdav_segment_cache_enabled")
    if enabled is None:
        return None
    if not enabled:
        return CacheStats(enabled=False)
    hits = metrics.value("nzbdav_segment_cache_hits_total") or 0
    misses = metrics.value("nzbdav_segment_cache_misses_total") or 0
    used = metrics.value("nzbdav_segment_cache_bytes")
    limit = metrics.value("nzbdav_segment_cache_max_bytes")
    entries = metrics.value("nzbdav_segment_cache_entries")
    return CacheStats(
        enabled=True,
        used_bytes=int(used) if used is not None else None,
        max_bytes=int(limit) if limit else None,
        entries=int(entries) if entries is not None else None,
        hit_rate=hits / (hits + misses) if hits + misses else None,  # since InfiniDysk started
    )


def _circuit(value: Any) -> CircuitState | None:
    return _CIRCUIT_WIRE.get(value) if isinstance(value, str) else None


def map_providers(
    usage: Any,
    overview: Mapping[str, Any] | None,
    metrics: Metrics | None,
    breakers: list[Any],
) -> list[ProviderStats]:
    """Configured providers (from provider usage), enriched with the freshest circuit state:
    metrics (this poll) > live breakers (≤ 5 s) > overview (≤ 30 s)."""
    rows = {
        key: row
        for row in _list((overview or {}).get("providers"))
        if (key := _str(_dict(row).get("provider")))
    }
    live = {key: b for b in breakers if (key := _str(_dict(b).get("provider")))}
    rescued = _dict((overview or {}).get("failover")).get("rescuedBy")
    saves = {
        key: _int(r.get("saves")) or 0
        for r in map(_dict, _list(rescued))
        if (key := _str(r.get("provider")))
    }
    metric_circuits = metrics.by_label("nzbdav_circuit_state", "provider_key") if metrics else {}
    # connectionBudget is often null (seen live), so the pool gauges come first.
    pool_active = (
        metrics.by_label("nzbdav_nntp_pool_connections", "provider_key", state="active")
        if metrics
        else {}
    )
    pool_max = (
        metrics.by_label("nzbdav_nntp_pool_max_connections", "provider_key", limit="effective")
        if metrics
        else {}
    )

    out = []
    for raw in _list(_dict(usage).get("providers")):
        item = _dict(raw)
        key = _str(item.get("providerId"))
        row = _dict(rows.get(key)) if key else {}
        breaker = _dict(live.get(key)) if key else {}
        circuit = _CIRCUIT_METRIC.get(int(metric_circuits[key])) if key in metric_circuits else None
        circuit = (
            circuit or _circuit(breaker.get("circuitState")) or _circuit(row.get("circuitState"))
        )
        budget = _dict(item.get("connectionBudget"))
        out.append(
            ProviderStats(
                name=_str(item.get("nickname")) or _str(item.get("host")) or "Provider",
                circuit=circuit or CircuitState.UNKNOWN,
                speed_mbps=_num(row.get("speedMbPerSec")),
                error_rate=_num(row.get("errorRate")) if row else None,
                last_failure=_str(breaker.get("lastFailureReason"))
                or _str(row.get("lastFailureReason")),
                connections_active=int(pool_active[key])
                if key in pool_active
                else _int(budget.get("activeTransferOperations")),
                connections_max=int(pool_max[key])
                if key in pool_max
                else _int(item.get("effectiveMaxConnections"))
                or _int(budget.get("effectiveTransferLimit")),
                over_limit=item.get("overLimit") is True,
                saves=saves.get(key, 0) if key and isinstance(rescued, list) else None,
            )
        )
    return out


def _throughput(overview: Mapping[str, Any]) -> list[float]:
    bucket_s = (_num(overview.get("throughputBucketSizeMs")) or 0) / 1000
    if bucket_s <= 0:
        return []
    points = sorted(
        (p for p in map(_dict, _list(overview.get("throughput"))) if _num(p.get("bucket"))),
        key=lambda p: p["bucket"],
    )
    return [(_num(p.get("bytesServed")) or 0) / bucket_s for p in points]


@dataclass
class LiveState:
    """What the backend WebSocket last reported (see ``ws.LiveFeed``)."""

    connected: bool = False
    error: str | None = None
    tiles: dict[str, Any] | None = None  # latest ``ls`` payload, None when stale
    reads: list[ActiveRead] = field(default_factory=list)
    recent: list[ActiveRead] = field(default_factory=list)  # files no longer being read


def build_stats(
    *,
    usage: Any,
    overview: Mapping[str, Any] | None = None,
    metrics: Metrics | None = None,
    live: LiveState | None = None,
    healthy: bool | None = None,
    ready: bool | None = None,
) -> InfiniDyskStats:
    live = live or LiveState()
    tiles = live.tiles or _dict((overview or {}).get("tiles"))
    active = _int(live.tiles.get("activeReads")) if live.tiles else None
    if active is None and metrics is not None:
        gauge = metrics.value("nzbdav_active_reads")
        active = int(gauge) if gauge is not None else None
    if active is None:
        active = _int(tiles.get("activeReads"))
    latency = _dict((overview or {}).get("latency"))
    has_latency = bool(_int(latency.get("samples")))
    return InfiniDyskStats(
        healthy=healthy,
        ready=ready,
        active_reads=active or 0,
        articles_per_min=_num(tiles.get("articlesPerMinute")),
        errors_per_min=_num(tiles.get("errorsPerMinute")),
        bytes_served_per_min=_num(tiles.get("bytesServedPerMinute")),
        throughput=_throughput(overview) if overview else [],
        latency_p50_ms=_num(latency.get("p50Ms")) if has_latency else None,
        latency_p95_ms=_num(latency.get("p95Ms")) if has_latency else None,
        latency_p99_ms=_num(latency.get("p99Ms")) if has_latency else None,
        providers=map_providers(
            usage, overview, metrics, _list((live.tiles or {}).get("providerBreakers"))
        ),
        cache=map_cache(metrics) if metrics is not None else None,
        reads=live.reads if live.connected else [],
        recent_reads=live.recent if live.connected else [],
        live=live.connected,
        feed_error=live.error,
        articles_recovered=_int(_dict((overview or {}).get("failover")).get("articlesRecovered")),
    )
