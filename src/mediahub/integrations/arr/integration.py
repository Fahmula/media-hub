"""Sonarr and Radarr integrations (API v3).

Phase 1: connection test, version and the app's own health checks. Queue and history
monitoring arrive in Phase 3. Auth is the ``X-Api-Key`` header only: a stray ``?apikey=``
query parameter would override it.
"""

from __future__ import annotations

from typing import ClassVar

from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    FieldSpec,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import get_json, make_client
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck

_HEALTH_LEVELS = {"notice": Severity.INFO, "warning": Severity.WARNING, "error": Severity.ERROR}


def map_health(items: object) -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    if not isinstance(items, list):
        return checks
    for item in items:
        if not isinstance(item, dict):
            continue
        level = _HEALTH_LEVELS.get(str(item.get("type", "")).lower())
        message = item.get("message")
        if level is None or not message:
            continue  # "ok" entries carry no information
        checks.append(
            HealthCheck(
                source=item.get("source"),
                level=level,
                message=str(message),
                wiki_url=item.get("wikiUrl"),
            )
        )
    return checks


class ArrIntegration(Integration):
    app_name: ClassVar[str]
    capabilities = frozenset({Capability.DOWNLOADS, Capability.HISTORY})
    default_interval_s = 10
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key", "API key", secret=True, required=True, help="Settings → General → API Key."
        ),
    )

    def __init__(self, config: InstanceConfig, source: SourceRef) -> None:
        super().__init__(config, source)
        self._client = make_client(
            config.base_url,
            headers={"X-Api-Key": config.secrets.get("api_key", "")},
            verify_tls=config.options.get("verify_tls", True),
        )

    async def _status(self) -> dict[str, object]:
        status = await get_json(self._client, "/api/v3/system/status")
        if not isinstance(status, dict) or "version" not in status:
            raise IntegrationError(ErrorKind.PROTOCOL, "Unexpected /api/v3/system/status response")
        return status

    async def test_connection(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=False)
        try:
            status = await self._status()
        except IntegrationError as exc:
            label = "API key accepted" if exc.kind is ErrorKind.AUTH else f"Reach {self.app_name}"
            result.add(label, False, exc.message)
            return result
        app = str(status.get("appName") or "")
        result.version = str(status.get("version"))
        result.server_name = str(status.get("instanceName") or app)
        if app and app.lower() != self.app_name.lower():
            result.add(f"Reach {self.app_name}", False, f"This URL is {app}, not {self.app_name}")
            return result
        result.add(f"Reach {self.app_name}", True, f"{result.server_name} · v{result.version}")
        result.add("API key accepted", True)
        result.ok = True
        return result

    async def poll(self, *, full: bool) -> PollResult:
        status = await self._status()
        checks = map_health(await get_json(self._client, "/api/v3/health"))
        degraded = any(c.level is Severity.ERROR for c in checks)
        return PollResult(
            status=ServiceStatus.DEGRADED if degraded else ServiceStatus.OK,
            version=str(status.get("version")),
            health_checks=checks,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class SonarrIntegration(ArrIntegration):
    service_type = ServiceType.SONARR
    app_name = "Sonarr"
    default_url = "http://sonarr:8989"


class RadarrIntegration(ArrIntegration):
    service_type = ServiceType.RADARR
    app_name = "Radarr"
    default_url = "http://radarr:7878"
