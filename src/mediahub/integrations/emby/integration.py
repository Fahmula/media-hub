"""Emby integration.

Phase 1: connection test and health polling. Session monitoring arrives in Phase 2.
API notes (Emby 4.x): token in ``X-Emby-Token``; all routes under ``/emby``;
``/System/Info/Public`` needs no auth and ``/System/Info`` validates the key.
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
from mediahub.models.common import ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck


def emby_api_root(base_url: str) -> str:
    """Accept both ``http://host:8096`` and ``http://host:8096/emby``."""
    root = base_url.rstrip("/")
    if root.lower().endswith("/emby"):
        root = root[: -len("/emby")]
    return f"{root}/emby"


class EmbyIntegration(Integration):
    service_type = ServiceType.EMBY
    capabilities = frozenset({Capability.PLAYBACK})
    default_interval_s = 5
    default_url = "http://emby:8096"
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key",
            "API key",
            secret=True,
            required=True,
            help="Emby → Settings → Advanced → API Keys.",
        ),
    )

    def __init__(self, config: InstanceConfig, source: SourceRef) -> None:
        super().__init__(config, source)
        self._client = make_client(
            emby_api_root(config.base_url),
            headers={"X-Emby-Token": config.secrets.get("api_key", "")},
            verify_tls=config.options.get("verify_tls", True),
        )

    async def test_connection(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=False)
        try:
            public = await get_json(self._client, "/System/Info/Public")
        except IntegrationError as exc:
            result.add("Reach Emby", False, exc.message)
            return result
        if not isinstance(public, dict) or "Version" not in public:
            result.add("Reach Emby", False, "Response doesn't look like an Emby server")
            return result
        result.version = public.get("Version")
        result.server_name = public.get("ServerName")
        result.add("Reach Emby", True, f"{result.server_name or 'Emby'} · v{result.version}")
        try:
            await get_json(self._client, "/System/Info")
        except IntegrationError as exc:
            result.add("API key accepted", False, exc.message)
            return result
        result.add("API key accepted", True)
        result.ok = True
        return result

    async def poll(self, *, full: bool) -> PollResult:
        info = await get_json(self._client, "/System/Info")
        if not isinstance(info, dict):
            raise IntegrationError(ErrorKind.PROTOCOL, "Unexpected /System/Info response")
        checks: list[HealthCheck] = []
        if info.get("HasPendingRestart"):
            checks.append(HealthCheck(level=Severity.INFO, message="Restart pending"))
        if info.get("HasUpdateAvailable"):
            checks.append(HealthCheck(level=Severity.INFO, message="Server update available"))
        return PollResult(version=info.get("Version"), health_checks=checks)

    async def aclose(self) -> None:
        await self._client.aclose()
