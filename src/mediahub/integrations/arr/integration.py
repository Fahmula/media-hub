"""Sonarr and Radarr integrations (API v3).

Every poll fetches status, health and the queue (issues depend on it); full polls add recent
history. Posters come from ``/api/v3/mediacover/{id}/poster-250.jpg``, which accepts the API
key (``/MediaCover/...`` doesn't). Auth is the ``X-Api-Key`` header only: a stray ``?apikey=``
query parameter would override it.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from mediahub.integrations.arr.mapper import POSTER_TAG, map_history, map_queue
from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    FieldSpec,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import get_json, make_client, request
from mediahub.integrations.image_refs import image_url, valid_part
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck

# Sonarr v4 returns one queue record per episode, so a few season packs add up quickly.
QUEUE_PAGE_SIZE = 500
HISTORY_PAGE_SIZE = 30
IMAGE_MAX_BYTES = 5 * 1024 * 1024

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
    queue_params: ClassVar[dict[str, str]]
    history_params: ClassVar[dict[str, str]]
    capabilities = frozenset({Capability.DOWNLOADS, Capability.HISTORY})
    default_interval_s = 10
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key", "API key", secret=True, required=True, help="Settings → General → API Key."
        ),
    )
    option_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "import_warning_grace_min",
            "Import warning delay (minutes)",
            kind="minutes",
            placeholder="10",
            help="How long an import warning must last before it's listed under Needs "
            "Attention. Raise it if downloads go through a post-processor (e.g. FileFlows) "
            "before import: until the converted file appears, the import is retried with a "
            'warning such as "No files found are eligible for import".',
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
        status, health, queue = await asyncio.gather(
            self._status(),
            get_json(self._client, "/api/v3/health"),
            self._page("/api/v3/queue", QUEUE_PAGE_SIZE, self.queue_params),
        )
        # History only matters while someone is looking; idle polls keep the last one.
        history = None
        if full:
            history = await self._page("/api/v3/history", HISTORY_PAGE_SIZE, self.history_params)
        checks = map_health(health)
        degraded = any(c.level is Severity.ERROR for c in checks)
        return PollResult(
            status=ServiceStatus.DEGRADED if degraded else ServiceStatus.OK,
            version=str(status.get("version")),
            health_checks=checks,
            downloads=map_queue(queue, self.source, self._image_url),
            history=map_history(history, self.source, self._image_url) if full else None,
            import_warning_grace_s=self._import_warning_grace_s(),
        )

    def _import_warning_grace_s(self) -> int | None:
        minutes = self.config.options.get("import_warning_grace_min")
        return minutes * 60 if isinstance(minutes, int) else None

    async def _page(self, path: str, size: int, params: dict[str, str]) -> list[Any]:
        query = {"page": "1", "pageSize": str(size), **params}
        page = await get_json(self._client, path, params=query)
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise IntegrationError(ErrorKind.PROTOCOL, f"Unexpected {path} response")
        records: list[Any] = page["records"]
        return records

    def _image_url(self, media_id: str, tag: str) -> str | None:
        return image_url(self.config.id, media_id, tag)

    async def fetch_image(self, ref: str) -> tuple[bytes, str] | None:
        media_id, _, tag = ref.partition("/")
        if not (media_id.isdigit() and valid_part(media_id) and tag == POSTER_TAG):
            return None
        response = await request(
            self._client,
            "GET",
            f"/api/v3/mediacover/{media_id}/{tag}.jpg",
            headers={"Accept": "image/*"},
        )
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/") or len(response.content) > IMAGE_MAX_BYTES:
            return None
        return response.content, content_type

    async def aclose(self) -> None:
        await self._client.aclose()


class SonarrIntegration(ArrIntegration):
    service_type = ServiceType.SONARR
    app_name = "Sonarr"
    default_url = "http://sonarr:8989"
    queue_params: ClassVar[dict[str, str]] = {
        "sortKey": "added",
        "sortDirection": "ascending",
        "includeUnknownSeriesItems": "true",
        "includeSeries": "true",
        "includeEpisode": "true",
    }
    history_params: ClassVar[dict[str, str]] = {
        "sortKey": "date",
        "sortDirection": "descending",
        "includeSeries": "true",
        "includeEpisode": "true",
    }


class RadarrIntegration(ArrIntegration):
    service_type = ServiceType.RADARR
    app_name = "Radarr"
    default_url = "http://radarr:7878"
    queue_params: ClassVar[dict[str, str]] = {
        "sortKey": "added",
        "sortDirection": "ascending",
        "includeUnknownMovieItems": "true",
        "includeMovie": "true",
    }
    history_params: ClassVar[dict[str, str]] = {
        "sortKey": "date",
        "sortDirection": "descending",
        "includeMovie": "true",
    }
