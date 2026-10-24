"""Sonarr/Radarr pages rendered from the constructed queue/history fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mediahub.integrations import image_refs
from mediahub.integrations.arr.mapper import map_history, map_queue
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.snapshot import ServiceSnapshot

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _snapshot(client: TestClient, app: str) -> ServiceSnapshot:
    identity = client.app.state.ctx.identity  # type: ignore[attr-defined]
    instance_id = f"{app}-1"
    source = SourceRef(
        server_id=identity.server_id,
        server_name=identity.server_name,
        instance_id=instance_id,
        instance_name=app.title(),
        service_type=ServiceType(app),
    )
    data: dict[str, Any] = json.loads((FIXTURES / app / "constructed.json").read_text())

    def image(media_id: str, tag: str) -> str | None:
        return image_refs.image_url(instance_id, media_id, tag)

    now = datetime.now(UTC)
    return ServiceSnapshot(
        source=source,
        status=ServiceStatus.OK,
        checked_at=now,
        fetched_at=now,
        downloads=map_queue(data["queue"]["records"], source, image),
        history=map_history(data["history"]["records"], source, image),
    )


def _put(client: TestClient, *apps: str) -> None:
    store = client.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    for app in apps:
        store.put_local(_snapshot(client, app))


def test_sonarr_page_renders_queue(authed: TestClient) -> None:
    _put(authed, "sonarr")
    html = authed.get("/partials/service/sonarr?server=local").text
    for text in [
        "Series 1",
        "S01E01 · Episode 1",
        "S02E01–E03",
        "3 episodes",
        "Import blocked",
        "The download is stalled with no connections",
        "WEBDL-1080p",
        "/img/sonarr-1/1/poster-250/",
    ]:
        assert text in html, text


def test_arr_problems_reach_needs_attention(authed: TestClient) -> None:
    _put(authed, "sonarr", "radarr")
    html = authed.get("/partials/attention").text
    assert "Series 4 · S02E01–E03" in html
    assert "Found matching movie via grab history" in html
    assert "Unpacking failed, write error or disk is full?" in html


def test_arr_history_on_activity(authed: TestClient) -> None:
    _put(authed, "sonarr", "radarr")
    html = authed.get("/partials/activity/history").text
    assert "Failed download detected" in html
    assert "S02E01–E03 · 3 episodes" in html
