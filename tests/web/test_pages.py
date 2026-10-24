from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mediahub.config import LogFormat, Settings
from mediahub.main import create_app

from .conftest import setup_admin

PAGES = [
    "/",
    "/activity",
    "/activity?tab=downloads",
    "/activity?tab=history",
    "/activity?tab=streams",
    "/attention",
    "/servers",
    "/emby",
    "/sonarr",
    "/radarr",
    "/infinidysk",
    "/partials/dashboard/streams",
    "/partials/dashboard/summary",
    "/partials/activity/downloads",
    "/partials/service/infinidysk",
]


@pytest.fixture
def demo_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        config_dir=tmp_path / "config", log_format=LogFormat.CONSOLE, demo_mode=True
    )
    with TestClient(create_app(settings), base_url="http://testserver") as c:
        setup_admin(c)
        deadline = time.monotonic() + 5
        while "Interstellar" not in c.get("/").text and time.monotonic() < deadline:
            time.sleep(0.05)
        yield c


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(demo_client: TestClient, path: str) -> None:
    resp = demo_client.get(path)
    assert resp.status_code == 200, resp.text[:500]


def test_dashboard_shows_both_servers_and_issues(demo_client: TestClient) -> None:
    html = demo_client.get("/").text
    assert "Brother Server" in html
    assert "Needs attention" in html
    assert "The download is stalled with no connections" in html


def test_scope_selection_is_remembered(demo_client: TestClient) -> None:
    resp = demo_client.get("/?server=local")
    assert "mh_server=local" in resp.headers.get("set-cookie", "")
    html = demo_client.get("/").text
    assert "Brother Server</p>" not in html  # local scope: no peer server card


def test_unknown_partial_404(demo_client: TestClient) -> None:
    assert demo_client.get("/partials/dashboard/nope").status_code == 404


def test_demo_poster_escapes_title(demo_client: TestClient) -> None:
    resp = demo_client.get("/img/demo/x.svg", params={"t": "<script>alert(1)</script>"})
    assert resp.status_code == 200
    assert "<script>" not in resp.text
