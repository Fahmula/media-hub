"""InfiniDysk: metrics parsing, mapping, polling and the live WebSocket feed.

Fixtures are constructed from the InfiniDysk source (see tests/fixtures/infinidysk).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from websockets.asyncio.server import ServerConnection, serve

from mediahub.integrations.base import InstanceConfig
from mediahub.integrations.infinidysk import integration as idk
from mediahub.integrations.infinidysk.integration import InfiniDyskIntegration
from mediahub.integrations.infinidysk.mapper import (
    LiveState,
    build_stats,
    client_label,
    map_read,
)
from mediahub.integrations.infinidysk.metrics import Metrics, parse
from mediahub.integrations.infinidysk.ws import IDLE_LINGER_S, LiveFeed
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.infinidysk import CircuitState
from mediahub.models.issues import IssueCategory
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.issues import derive_issues

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "infinidysk" / "constructed.json").read_text()
)
SRC = SourceRef(
    server_id="srv",
    server_name="Skynet",
    instance_id="idk-1",
    instance_name="InfiniDysk",
    service_type=ServiceType.INFINIDYSK,
)
FRONT = "http://idk:3000"
BACK = "http://idk:8080"


def frame(topic: str) -> str:
    return json.dumps(next(f for f in FIXTURE["ws_frames"] if f["Topic"] == topic))


# ----- metrics -------------------------------------------------------------------------------
def test_metrics_parser() -> None:
    samples = parse(
        'nzbdav_x{a="q\\"uo\\\\te",b="line\\nbreak"} 1.5\n'
        "nzbdav_y 2 1700000000000\n"
        "nzbdav_nan NaN\n"
        "# HELP nzbdav_x help\n"
        "other_metric 3\n"
    )
    assert [(s.name, s.value) for s in samples] == [("nzbdav_x", 1.5), ("nzbdav_y", 2.0)]
    assert samples[0].labels == {"a": 'q"uo\\te', "b": "line\nbreak"}

    metrics = Metrics.from_text(FIXTURE["metrics"])
    assert metrics.value("nzbdav_active_reads") == 2
    assert metrics.by_label("nzbdav_circuit_state", "provider_key") == {"key-a": 0, "key-b": 1}
    assert metrics.value("nzbdav_nntp_pool_connections", provider_key="key-a", state="idle") is None


# ----- mapping -------------------------------------------------------------------------------
def test_idle_stats_from_usage_and_metrics() -> None:
    stats = build_stats(
        usage=FIXTURE["provider_usage"],
        metrics=Metrics.from_text(FIXTURE["metrics"]),
        healthy=True,
        ready=True,
    )
    assert stats.active_reads == 2  # nzbdav_active_reads gauge
    assert stats.cache is not None and stats.cache.hit_rate == 0.75
    assert stats.cache.used_pct == 50
    primary, block, legacy = stats.providers
    assert (primary.name, primary.circuit) == ("Primary", CircuitState.CLOSED)
    assert (primary.connections_active, primary.connections_max) == (12, 50)
    assert (block.name, block.circuit, block.over_limit) == (
        "block.example.net",
        CircuitState.OPEN,
        True,
    )
    assert block.connections_active is None and block.connections_max == 20
    assert (legacy.name, legacy.circuit) == ("legacy.example.org", CircuitState.UNKNOWN)
    assert stats.throughput == [] and stats.reads == [] and not stats.live


def test_full_stats_prefer_the_freshest_source() -> None:
    feed = LiveFeed("ws://unused", "k")
    feed.handle(frame("ls"))
    feed.handle(frame("ar"))
    feed.connected = True
    stats = build_stats(
        usage=FIXTURE["provider_usage"],
        overview=FIXTURE["overview"],
        metrics=Metrics.from_text(FIXTURE["metrics"]),
        live=feed.state(),
    )
    assert stats.active_reads == 1  # live tile, not the gauge (2) or the overview tile (2)
    assert stats.articles_per_min == 2400
    assert stats.live and len(stats.reads) == 1
    # Throughput: oldest first, bytes per second (bytesServed per 60 s bucket).
    assert len(stats.throughput) == 60
    assert stats.throughput[0] == 1048576 and stats.throughput[-1] == 60 * 1048576
    assert (stats.latency_p50_ms, stats.latency_p95_ms, stats.latency_p99_ms) == (41, 118, 262)
    primary, block, _ = stats.providers
    # Metrics win over the live breaker and the overview (which says halfOpen).
    assert primary.circuit is CircuitState.CLOSED
    assert primary.speed_mbps == 42.5 and primary.error_rate == 0.00004
    assert block.last_failure == "502 Authentication failed: account expired (live)"
    # Without metrics, the live breakers decide; without those, the overview.
    no_metrics = build_stats(usage=FIXTURE["provider_usage"], overview=FIXTURE["overview"])
    assert no_metrics.providers[0].circuit is CircuitState.HALF_OPEN
    assert no_metrics.active_reads == 2


def test_read_mapping() -> None:
    raw = json.loads(json.loads(frame("ar"))["Message"])["reads"][0]
    read = map_read(raw, speed_bps=1e6)
    assert read.file_name == "Movie.1.2026.2160p.WEB-DL.mkv"
    assert read.client_label == "Emby"
    assert read.offset_pct == 50
    assert read.started_at is not None and read.started_at.year == 2026
    assert read.path and read.client_ip  # local only; peers/redact.py strips them
    assert client_label("Lavf/61.7.100") == "FFmpeg"
    assert client_label("SomeApp/1.0 (Linux)") == "SomeApp"
    assert client_label(None) is None


def test_read_speed_from_successive_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = LiveFeed("ws://unused", "k")
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    now_ms = time.time() * 1000

    def reads(bytes_read: int) -> str:
        read = {"id": "r1", "fileName": "f", "bytesRead": bytes_read, "lastActivityAt": now_ms}
        return json.dumps({"Topic": "ar", "Message": json.dumps({"reads": [read]})})

    feed.handle(reads(0))
    assert feed.state().reads[0].speed_bps is None  # one sample: no rate yet
    clock[0] += 4
    feed.handle(reads(40_000_000))
    assert feed.state().reads[0].speed_bps == 10_000_000
    feed.handle(json.dumps({"Topic": "ar", "Message": json.dumps({"reads": []})}))
    [idle] = feed.state().reads  # dropped by InfiniDysk, kept as idle
    assert idle.idle and idle.speed_bps == 0 and feed._samples == {}
    feed.handle("not json")
    feed.handle(json.dumps({"Topic": "ls", "Message": "[1, 2]"}))  # ignored, not a crash


def test_provider_issues() -> None:
    stats = build_stats(
        usage=FIXTURE["provider_usage"], metrics=Metrics.from_text(FIXTURE["metrics"])
    )
    snap = ServiceSnapshot(source=SRC, status=ServiceStatus.OK, infinidysk=stats)
    titles = {i.title for i in derive_issues(snap) if i.category is IssueCategory.PROVIDER}
    assert titles == {
        "Provider block.example.net circuit open",
        "Provider block.example.net reached its byte limit",
    }


# ----- polling -------------------------------------------------------------------------------
def _integration(**secrets: str) -> InfiniDyskIntegration:
    cfg = InstanceConfig(
        id="idk-1",
        service_type=ServiceType.INFINIDYSK,
        name="InfiniDysk",
        base_url=FRONT,
        secrets={"api_key": "user-key", **secrets},
        options={"backend_url": BACK},
    )
    return InfiniDyskIntegration(cfg, SRC)


def _mock(router: Any = respx) -> dict[str, respx.Route]:
    return {
        "usage": router.get(f"{FRONT}/api/get-provider-usage").mock(
            return_value=httpx.Response(200, json=FIXTURE["provider_usage"])
        ),
        "overview": router.get(f"{FRONT}/api/get-overview-stats").mock(
            return_value=httpx.Response(200, json=FIXTURE["overview"])
        ),
        "health": router.get(f"{BACK}/health").mock(return_value=httpx.Response(200, text="OK")),
        "ready": router.get(f"{BACK}/ready").mock(return_value=httpx.Response(200, text="OK")),
        "metrics": router.get(f"{BACK}/metrics").mock(
            return_value=httpx.Response(200, text=FIXTURE["metrics"])
        ),
    }


@respx.mock
async def test_overview_only_while_viewed_and_throttled() -> None:
    routes = _mock()
    integration = _integration()
    idle = await integration.poll(full=False)
    assert routes["overview"].call_count == 0 and routes["metrics"].call_count == 1
    assert idle.infinidysk is not None and idle.infinidysk.throughput == []

    full = await integration.poll(full=True)
    params = routes["overview"].calls.last.request.url.params
    assert (params["window"], params["sections"]) == ("1h", "window,detail")
    assert routes["overview"].calls.last.request.headers["x-api-key"] == "user-key"
    assert full.infinidysk is not None and len(full.infinidysk.throughput) == 60
    await integration.poll(full=True)
    assert routes["overview"].call_count == 1  # cached for OVERVIEW_EVERY_S
    await integration.aclose()


@respx.mock
async def test_optional_sources_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _mock()
    routes["metrics"].mock(
        return_value=httpx.Response(401, text="Metrics authentication required.")
    )
    routes["overview"].mock(return_value=httpx.Response(500, json={"status": False, "error": "x"}))
    result = await _integration().poll(full=True)
    assert result.status is ServiceStatus.DEGRADED
    messages = [c.message for c in result.health_checks]
    assert any(m.startswith("Metrics unavailable") for m in messages)
    assert any(m.startswith("Overview statistics unavailable") for m in messages)
    assert result.infinidysk is not None and result.infinidysk.cache is None


# ----- live feed -----------------------------------------------------------------------------
@pytest.fixture
async def backend() -> AsyncIterator[tuple[str, list[str]]]:
    """A minimal InfiniDysk /ws: key first, then a subscription, then topic frames."""
    received: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        key = await ws.recv()
        received.append(str(key))
        if key != "internal-key":
            await ws.close(1008, "Unauthorized")
            return
        received.append(str(await ws.recv()))
        await ws.send(frame("ar"))
        await ws.send(frame("ls"))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/ws", received


async def _wait(cond: Any) -> None:
    async with asyncio.timeout(3):
        while not cond():  # noqa: ASYNC110 - polls state set by the feed task
            await asyncio.sleep(0.02)


async def test_live_feed_subscribes_and_tracks_state(backend: tuple[str, list[str]]) -> None:
    url, received = backend
    feed = LiveFeed(url, "internal-key")
    feed.start()
    await _wait(lambda: feed.state().tiles is not None)
    state = feed.state()
    assert received == ["internal-key", '{"sub": ["ls", "ar"]}']
    assert state.connected and state.error is None
    assert [r.file_name for r in state.reads] == ["Movie.1.2026.2160p.WEB-DL.mkv"]
    await feed.stop()
    assert not feed.running and feed.state() == LiveState()


async def test_live_feed_reports_rejected_key(backend: tuple[str, list[str]]) -> None:
    url, _ = backend
    feed = LiveFeed(url, "wrong")
    feed.start()
    await _wait(lambda: feed.error is not None)
    assert feed.error == "Internal key rejected by InfiniDysk" and not feed.connected
    await feed.stop()


async def test_idle_poll_stops_the_feed(backend: tuple[str, list[str]]) -> None:
    url, _ = backend
    integration = _integration(internal_key="internal-key")
    integration._feed = LiveFeed(url, "internal-key")
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock)
        await integration.poll(full=True)
        assert integration._feed.running
        await _wait(lambda: integration._feed is not None and integration._feed.connected)
        full = await integration.poll(full=True)
        assert full.infinidysk is not None and full.infinidysk.live
        assert full.infinidysk.active_reads == 1
        await integration.poll(full=False)
        assert not integration._feed.running
    await integration.aclose()


def test_overview_constants() -> None:
    assert idk.OVERVIEW_EVERY_S >= 30  # matches InfiniDysk's own UI refresh


# ----- real capture --------------------------------------------------------------------------
def test_live_capture_maps_cleanly() -> None:
    """Captured from a real InfiniDysk (scripts/capture-infinidysk.py, redacted)."""
    live: dict[str, Any] = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "infinidysk" / "live.json").read_text()
    )
    feed = LiveFeed("ws://unused", "k")
    for f in live["ws_frames"]:
        feed.handle(json.dumps(f))
    feed.connected = True
    stats = build_stats(
        usage=live["provider_usage"],
        overview=live["overview"],
        metrics=Metrics.from_text(live["metrics"]),
        live=feed.state(),
    )
    assert [p.name for p in stats.providers] == ["Provider 1", "Provider 2"]
    assert all(p.circuit is CircuitState.CLOSED for p in stats.providers)
    # connectionBudget was null for both: connections come from the pool gauges.
    assert all(p.connections_active == 0 and p.connections_max == 15 for p in stats.providers)
    assert stats.cache is not None and not stats.cache.enabled
    assert len(stats.throughput) == 59 and stats.latency_p95_ms == 729
    assert stats.articles_per_min == 560  # from the live ls tile
    assert stats.live and stats.reads == []


def test_recorded_playback_keeps_buffered_read_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replays 5 min of real frames: Emby buffers ~850 MB, seeks once, then plays from its
    buffer; InfiniDysk drops the read at t=105 s although playback goes on."""
    recorded = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "infinidysk" / "streaming.json").read_text()
    )
    frames = recorded["ws_frames"]
    first = next(f for f in frames if f["Topic"] == "ar" and json.loads(f["Message"])["reads"])
    # Wall clock at t=0, from the first read's lastActivityAt (it was ~0.1 s idle then).
    start = json.loads(first["Message"])["reads"][0]["lastActivityAt"] / 1000 - first["t"]
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: 1000 + clock["t"])
    monkeypatch.setattr(time, "time", lambda: start + clock["t"])

    feed = LiveFeed("ws://unused", "k")
    feed.connected = True
    seen: dict[float, LiveState] = {}
    for f in frames:
        clock["t"] = f["t"]
        feed.handle(json.dumps({"Topic": f["Topic"], "Message": f["Message"]}))
        if f["Topic"] == "ar":
            seen[f["t"]] = feed.state()

    def at(t: float) -> LiveState:
        return seen[min(seen, key=lambda k: abs(k - t))]

    [buffering] = at(28).reads
    assert buffering.client_label == "FFmpeg" and not buffering.idle
    assert buffering.speed_bps is not None and buffering.speed_bps > 20e6  # tens of MB/s
    [after_seek] = at(60).reads
    assert after_seek.offset_pct is not None and 50 < after_seek.offset_pct < 60
    [dropped] = at(105.3).reads  # InfiniDysk sent {"reads": []}
    assert dropped.idle and dropped.speed_bps == 0 and dropped.read_id == buffering.read_id

    clock["t"] = 300
    assert [r.idle for r in feed.state().reads] == [True]
    clock["t"] = 90 + IDLE_LINGER_S + 1  # 10 min after its last data
    assert feed.state().reads == []


def test_idle_read_replaced_when_the_file_is_read_again() -> None:
    feed = LiveFeed("ws://unused", "k")

    def ar(*reads: dict[str, Any]) -> str:
        return json.dumps({"Topic": "ar", "Message": json.dumps({"reads": list(reads)})})

    now_ms = time.time() * 1000
    feed.handle(
        ar({"id": "old", "path": "/c/f.mkv", "fileName": "f.mkv", "lastActivityAt": now_ms})
    )
    feed.handle(ar())
    assert [(r.read_id, r.idle) for r in feed.state().reads] == [("old", True)]
    feed.handle(
        ar({"id": "new", "path": "/c/f.mkv", "fileName": "f.mkv", "lastActivityAt": now_ms})
    )
    assert [(r.read_id, r.idle) for r in feed.state().reads] == [("new", False)]
