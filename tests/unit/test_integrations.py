from __future__ import annotations

import httpx
import pytest
import respx

from mediahub.integrations.arr.integration import SonarrIntegration, map_health
from mediahub.integrations.base import InstanceConfig
from mediahub.integrations.emby.integration import EmbyIntegration, emby_api_root
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.infinidysk.integration import assert_read_only, ws_url
from mediahub.models.common import ServiceType, SourceRef
from mediahub.models.issues import Severity


def _src(st: ServiceType) -> SourceRef:
    return SourceRef(
        server_id="s", server_name="S", instance_id="i", instance_name="I", service_type=st
    )


def sonarr(url: str = "http://sonarr:8989") -> SonarrIntegration:
    cfg = InstanceConfig(
        id="i", service_type=ServiceType.SONARR, name="TV", base_url=url, secrets={"api_key": "K"}
    )
    return SonarrIntegration(cfg, _src(ServiceType.SONARR))


def test_emby_api_root() -> None:
    assert emby_api_root("http://h:8096") == "http://h:8096/emby"
    assert emby_api_root("http://h:8096/emby/") == "http://h:8096/emby"


@respx.mock
async def test_sonarr_connection_ok_and_header_auth() -> None:
    route = respx.get("http://sonarr:8989/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.20"})
    )
    result = await sonarr().test_connection()
    assert result.ok and result.version == "4.0.20"
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "K"
    assert "apikey" not in str(request.url)


@respx.mock
async def test_sonarr_detects_wrong_app() -> None:
    respx.get("http://sonarr:8989/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"appName": "Radarr", "version": "6.0"})
    )
    result = await sonarr().test_connection()
    assert not result.ok
    assert "not Sonarr" in (result.steps[-1].detail or "")


@respx.mock
async def test_errors_are_translated() -> None:
    respx.get("http://sonarr:8989/api/v3/system/status").mock(return_value=httpx.Response(401))
    with pytest.raises(IntegrationError) as exc:
        await sonarr().poll(full=True)
    assert exc.value.kind is ErrorKind.AUTH

    respx.get("http://sonarr:8989/api/v3/system/status").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    with pytest.raises(IntegrationError) as exc:
        await sonarr().poll(full=True)
    assert exc.value.kind is ErrorKind.UNREACHABLE

    respx.get("http://sonarr:8989/api/v3/system/status").mock(
        return_value=httpx.Response(302, headers={"location": "/login"})
    )
    with pytest.raises(IntegrationError) as exc:
        await sonarr().poll(full=True)
    assert "base URL" in exc.value.message


@respx.mock
async def test_emby_connection() -> None:
    respx.get("http://emby:8096/emby/System/Info/Public").mock(
        return_value=httpx.Response(200, json={"ServerName": "Skynet", "Version": "4.9.1"})
    )
    respx.get("http://emby:8096/emby/System/Info").mock(return_value=httpx.Response(401))
    cfg = InstanceConfig(
        id="e",
        service_type=ServiceType.EMBY,
        name="E",
        base_url="http://emby:8096",
        secrets={"api_key": "bad"},
    )
    result = await EmbyIntegration(cfg, _src(ServiceType.EMBY)).test_connection()
    assert not result.ok
    assert [s.ok for s in result.steps] == [True, False]


def test_map_health_skips_ok_and_maps_levels() -> None:
    checks = map_health(
        [
            {"source": "A", "type": "ok", "message": ""},
            {"source": "B", "type": "warning", "message": "Indexer down", "wikiUrl": "https://w"},
            {"source": "C", "type": "error", "message": "Root folder missing"},
            "garbage",
        ]
    )
    assert [(c.source, c.level) for c in checks] == [
        ("B", Severity.WARNING),
        ("C", Severity.ERROR),
    ]


@pytest.mark.parametrize(
    ("path", "params", "backend", "allowed"),
    [
        ("/api/get-overview-stats", None, False, True),
        ("/api/get-active-health-checks", None, False, True),
        ("/api/get-health-check-history", {"pageSize": "100"}, False, True),
        ("/api/recheck-file", None, False, False),
        ("/api/requeue-action-needed-health-checks", None, False, False),
        ("/api/db-backup-create", None, False, False),
        ("/api/reset-health-check-queue", None, False, False),
        ("/api", {"mode": "queue"}, False, True),
        ("/api", {"mode": "queue", "name": "delete", "value": "x"}, False, False),
        ("/api", {"mode": "pause"}, False, False),
        ("/metrics", None, True, True),
        ("/api/get-overview-stats", None, True, False),
    ],
)
def test_infinidysk_read_only_allowlist(
    path: str, params: dict[str, str] | None, backend: bool, allowed: bool
) -> None:
    if allowed:
        assert_read_only(path, params, backend=backend)
    else:
        with pytest.raises(IntegrationError):
            assert_read_only(path, params, backend=backend)


def test_ws_url() -> None:
    assert ws_url("http://infinidysk:8080") == "ws://infinidysk:8080/ws"
    assert ws_url("https://h/base/") == "wss://h/base/ws"
