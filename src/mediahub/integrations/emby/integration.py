"""Emby integration.

API notes (Emby 4.x): token in ``X-Emby-Token``; all routes under ``/emby``;
``/System/Info/Public`` needs no auth and ``/System/Info`` validates the key. ``/Sessions``
lists every connected client; only entries with a ``NowPlayingItem`` are playing.

``/Sessions`` has no ``MediaSources``. For a STRM item (``Path`` ends in ``.strm``) the URL it
points to is only in ``/Users/{user}/Items/{item}`` → ``MediaSources[].Path``. Its last path
segment is InfiniDysk's item id, which is how a stream is matched to the InfiniDysk read (the
.strm file itself is usually renamed by Sonarr/Radarr). Looked up once per item and cached.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    FieldSpec,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.emby.mapper import map_session, strm_target
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import get_json, make_client, request
from mediahub.integrations.image_refs import image_url, valid_part
from mediahub.models.common import ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import HealthCheck

IMAGE_MAX_WIDTH = 300
IMAGE_MAX_BYTES = 5 * 1024 * 1024
STRM_CACHE_MAX = 500
STRM_RETRY_S = 300.0  # a failed lookup is retried after this long


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
        # (item id, media source id) -> (target segment or None, monotonic time looked up)
        self._strm: dict[tuple[str, str], tuple[str | None, float]] = {}

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
        # Sessions are one cheap request, so idle polls fetch them too: the dashboard is
        # correct the moment someone opens it.
        sessions = await get_json(self._client, "/Sessions")
        if not isinstance(sessions, list):
            raise IntegrationError(ErrorKind.PROTOCOL, "Unexpected /Sessions response")
        return PollResult(
            version=info.get("Version"),
            health_checks=checks,
            sessions=await self._with_strm_targets(sessions, self.map_sessions(sessions)),
        )

    async def _with_strm_targets(
        self, raw: list[Any], sessions: list[PlaybackSession]
    ) -> list[PlaybackSession]:
        wanted: dict[str, tuple[str, str, str]] = {}  # session id -> (user, item, source)
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("NowPlayingItem"), dict):
                continue
            item = entry["NowPlayingItem"]
            path, user, item_id = item.get("Path"), entry.get("UserId"), item.get("Id")
            source = (entry.get("PlayState") or {}).get("MediaSourceId") or ""
            if (
                isinstance(path, str)
                and path.lower().endswith(".strm")
                and isinstance(user, str)
                and isinstance(item_id, str)
                and valid_part(user)
                and valid_part(item_id)
            ):
                wanted[str(entry.get("Id"))] = (user, item_id, str(source))
        if not wanted:
            return sessions
        now = time.monotonic()
        missing = {
            (user, item, source)
            for user, item, source in wanted.values()
            if (cached := self._strm.get((item, source))) is None
            or (cached[0] is None and now - cached[1] > STRM_RETRY_S)
        }
        if missing:
            await asyncio.gather(*(self._lookup_strm(*m, now) for m in missing))
        out = []
        for s in sessions:
            key = wanted.get(s.session_id)
            target = self._strm.get((key[1], key[2]), (None, 0.0))[0] if key else None
            out.append(s.model_copy(update={"strm_target": target}) if target else s)
        return out

    async def _lookup_strm(self, user: str, item_id: str, source_id: str, now: float) -> None:
        target = None
        try:
            item = await get_json(
                self._client, f"/Users/{user}/Items/{item_id}", params={"Fields": "MediaSources"}
            )
        except IntegrationError:
            item = None
        raw = item.get("MediaSources") if isinstance(item, dict) else None
        sources = [s for s in raw or [] if isinstance(s, dict)]
        chosen = next((s for s in sources if s.get("Id") == source_id), None) or (
            sources[0] if sources else None
        )
        if chosen is not None:
            target = strm_target(
                chosen.get("Path") if isinstance(chosen.get("Path"), str) else None
            )
        if len(self._strm) >= STRM_CACHE_MAX:
            self._strm.clear()
        self._strm[(item_id, source_id)] = (target, now)

    def map_sessions(self, raw: list[Any]) -> list[PlaybackSession]:
        instance_id = self.config.id
        mapped = (
            map_session(s, self.source, lambda item, tag: image_url(instance_id, item, tag))
            for s in raw
        )
        return [s for s in mapped if s is not None]

    async def fetch_image(self, ref: str) -> tuple[bytes, str] | None:
        item_id, _, tag = ref.partition("/")
        if not (valid_part(item_id) and valid_part(tag)):
            return None
        response = await request(
            self._client,
            "GET",
            f"/Items/{item_id}/Images/Primary",
            params={"tag": tag, "maxWidth": IMAGE_MAX_WIDTH, "quality": 90},
            headers={"Accept": "image/*"},
        )
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/") or len(response.content) > IMAGE_MAX_BYTES:
            return None
        return response.content, content_type

    async def aclose(self) -> None:
        await self._client.aclose()
