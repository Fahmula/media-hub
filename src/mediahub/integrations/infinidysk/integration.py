"""InfiniDysk integration.

InfiniDysk exposes two HTTP surfaces:

* the frontend (port 3000) proxies ``/api/*`` and accepts the user API key (``x-api-key``);
* the backend (port 8080, internal) serves ``/health``, ``/ready``, ``/metrics`` and the
  ``/ws`` WebSocket, which only accepts the internal ``FRONTEND_BACKEND_API_KEY``.

Several InfiniDysk GET routes change state (backups, queue resets, SAB pause/delete), so
every request goes through :meth:`InfiniDyskIntegration._get`, which enforces a read-only
allowlist.

Every poll reads provider usage (frontend) and, with a backend URL, ``/health``, ``/ready`` and
``/metrics``: cheap, and enough for issues (circuits, byte limits, health). While someone is
looking, polls add the overview statistics (at most every 30 s, like InfiniDysk's own UI;
the query is not free) and keep the live WebSocket feed open. See ``mapper.py`` for fields.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    FieldSpec,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import USER_AGENT, get_json, make_client, request
from mediahub.integrations.infinidysk.mapper import build_stats
from mediahub.integrations.infinidysk.metrics import Metrics
from mediahub.integrations.infinidysk.ws import LiveFeed
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck

# Read-only routes only. Anything else is refused before a request is made.
FRONTEND_READ_PATHS = frozenset(
    {"/api/get-overview-stats", "/api/get-provider-usage", "/api/get-arr-health", "/api/get-logs"}
)
SAB_READ_MODES = frozenset({"queue", "history", "warnings", "status", "server_stats"})
BACKEND_READ_PATHS = frozenset({"/health", "/ready", "/metrics"})

OVERVIEW_PARAMS = {"window": "1h", "sections": "window,detail"}
OVERVIEW_EVERY_S = 30.0
OVERVIEW_MAX_AGE_S = 90.0  # keep showing the last overview through a failed refresh


def assert_read_only(path: str, params: dict[str, Any] | None, *, backend: bool) -> None:
    if backend:
        allowed = path in BACKEND_READ_PATHS
    elif path == "/api":
        p = params or {}
        # SAB "queue"/"history" become deletes when given name=delete.
        allowed = p.get("mode") in SAB_READ_MODES and "name" not in p and "value" not in p
    else:
        allowed = path in FRONTEND_READ_PATHS
    if not allowed:
        raise IntegrationError(ErrorKind.PROTOCOL, f"Refusing non-allowlisted request to {path}")


def ws_url(backend_url: str) -> str:
    parts = urlsplit(backend_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws", "", ""))


class InfiniDyskIntegration(Integration):
    service_type = ServiceType.INFINIDYSK
    capabilities = frozenset({Capability.STREAM_STATS})
    default_interval_s = 10
    default_url = "http://infinidysk:3000"
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key",
            "API key",
            secret=True,
            required=True,
            help="InfiniDysk → Settings → SABnzbd → API Key.",
        ),
        FieldSpec(
            "internal_key",
            "Internal API key",
            secret=True,
            help="The FRONTEND_BACKEND_API_KEY from InfiniDysk's container settings. Pin it "
            "there so it doesn't change on restart. Enables the live stream list.",
        ),
    )
    option_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "backend_url",
            "Backend URL",
            kind="url",
            placeholder="http://infinidysk:8080",
            help="InfiniDysk's internal backend port (8080). Needed for health, cache metrics "
            "and live streams. Put both containers on the same Docker network.",
        ),
    )

    def __init__(self, config: InstanceConfig, source: SourceRef) -> None:
        super().__init__(config, source)
        verify = config.options.get("verify_tls", True)
        headers = {"x-api-key": config.secrets.get("api_key", "")}
        self._client = make_client(config.base_url, headers=headers, verify_tls=verify)
        backend_url = config.options.get("backend_url")
        self._backend_url: str | None = backend_url or None
        self._backend = (
            make_client(backend_url, headers=headers, verify_tls=verify) if backend_url else None
        )
        internal_key = config.secrets.get("internal_key")
        self._feed = (
            LiveFeed(ws_url(backend_url), internal_key) if backend_url and internal_key else None
        )
        self._overview: dict[str, Any] | None = None
        self._overview_at = 0.0

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert_read_only(path, params, backend=False)
        return await get_json(self._client, path, params=params)

    async def _backend_text(self, path: str) -> str:
        assert self._backend is not None
        assert_read_only(path, None, backend=True)
        response = await request(self._backend, "GET", path, headers={"Accept": "text/plain"})
        return response.text

    async def _backend_status(self, path: str) -> bool:
        """``/health`` and ``/ready`` return 503 with 'Unhealthy' when failing."""
        assert self._backend is not None
        assert_read_only(path, None, backend=True)
        try:
            response = await self._backend.get(path, headers={"Accept": "text/plain"})
        except httpx.TransportError as exc:
            raise IntegrationError(ErrorKind.UNREACHABLE, "Backend URL unreachable") from exc
        return response.status_code == 200 and "unhealthy" not in response.text.lower()

    async def _check_websocket(self) -> tuple[bool, str]:
        key = self.config.secrets.get("internal_key")
        if not key or not self._backend_url:
            return False, "Not configured"
        try:
            async with ws_connect(
                ws_url(self._backend_url),
                open_timeout=4,
                close_timeout=1,
                user_agent_header=USER_AGENT,
            ) as ws:
                await ws.send(key)
                # Authenticated and idle is fine: InfiniDysk only pushes after a subscription.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=2)
                return True, "Live stream feed available"
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            return (
                False,
                f"Internal key rejected (connection closed{f', code {code}' if code else ''})",
            )
        except InvalidStatus as exc:
            return False, f"WebSocket refused (HTTP {exc.response.status_code})"
        except (InvalidHandshake, OSError, TimeoutError):
            return False, "Could not open the WebSocket on the backend URL"

    async def test_connection(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=False)
        try:
            usage = await self._get("/api/get-provider-usage")
        except IntegrationError as exc:
            label = "API key accepted" if exc.kind is ErrorKind.AUTH else "Reach InfiniDysk"
            result.add(label, False, exc.message)
            return result
        providers = usage.get("providers") if isinstance(usage, dict) else None
        if not isinstance(providers, list):
            result.add("Reach InfiniDysk", False, "Response doesn't look like InfiniDysk")
            return result
        result.add("Reach InfiniDysk", True)
        result.add("API key accepted", True, f"{len(providers)} Usenet provider(s) configured")
        result.ok = True

        if self._backend is None:
            result.add("Backend URL", False, "Not set: health and cache metrics unavailable")
            return result
        try:
            healthy = await self._backend_status("/health")
            result.add("Backend health", healthy, "Healthy" if healthy else "Unhealthy")
        except IntegrationError as exc:
            result.add("Backend health", False, exc.message)
            return result
        try:
            metrics = await self._backend_text("/metrics")
            series = sum(1 for line in metrics.splitlines() if line.startswith("nzbdav_"))
            result.add("Prometheus metrics", series > 0, f"{series} series")
        except IntegrationError as exc:
            result.add("Prometheus metrics", False, exc.message)
        ok, detail = await self._check_websocket()
        result.add("Live stream feed", ok, detail)
        return result

    async def poll(self, *, full: bool) -> PollResult:
        usage = await self._get("/api/get-provider-usage")  # also reachability + auth
        checks: list[HealthCheck] = []
        healthy = ready = None
        metrics = None
        if self._backend is not None:
            healthy, ready = await asyncio.gather(
                self._backend_status("/health"), self._backend_status("/ready")
            )
            if not healthy:
                checks.append(
                    HealthCheck(level=Severity.ERROR, message="Backend reports Unhealthy")
                )
            if not ready:
                checks.append(
                    HealthCheck(level=Severity.WARNING, message="Streaming readiness check failing")
                )
            try:
                metrics = Metrics.from_text(await self._backend_text("/metrics"))
            except IntegrationError as exc:
                checks.append(
                    HealthCheck(
                        level=Severity.WARNING, message=f"Metrics unavailable: {exc.message}"
                    )
                )

        overview = None
        if full:
            overview = await self._fresh_overview(checks)
            if self._feed is not None:
                self._feed.start()
        elif self._feed is not None:
            await self._feed.stop()  # nobody is looking: no live feed, no overview queries

        status = ServiceStatus.DEGRADED if checks else ServiceStatus.OK
        return PollResult(
            status=status,
            health_checks=checks,
            infinidysk=build_stats(
                usage=usage,
                overview=overview,
                metrics=metrics,
                live=self._feed.state() if self._feed is not None and full else None,
                healthy=healthy,
                ready=ready,
            ),
        )

    async def _fresh_overview(self, checks: list[HealthCheck]) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._overview is not None and now - self._overview_at < OVERVIEW_EVERY_S:
            return self._overview
        try:
            data = await self._get("/api/get-overview-stats", OVERVIEW_PARAMS)
        except IntegrationError as exc:
            checks.append(
                HealthCheck(
                    level=Severity.WARNING,
                    message=f"Overview statistics unavailable: {exc.message}",
                )
            )
            fresh_enough = now - self._overview_at <= OVERVIEW_MAX_AGE_S
            return self._overview if fresh_enough else None
        if not isinstance(data, dict):
            raise IntegrationError(
                ErrorKind.PROTOCOL, "Unexpected /api/get-overview-stats response"
            )
        self._overview, self._overview_at = data, now
        return data

    async def aclose(self) -> None:
        if self._feed is not None:
            await self._feed.stop()
        await self._client.aclose()
        if self._backend is not None:
            await self._backend.aclose()
