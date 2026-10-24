"""Sonarr/Radarr queue and history mapping.

Fixtures are constructed from the Sonarr v4 / Radarr v6 sources (see their ``_note``), not
captured: the interesting states can't be produced on demand.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mediahub.instances.forms import parse_instance_form
from mediahub.integrations import image_refs
from mediahub.integrations.arr.integration import (
    ArrIntegration,
    RadarrIntegration,
    SonarrIntegration,
)
from mediahub.integrations.arr.mapper import (
    episode_label,
    map_history,
    map_queue,
    map_state,
    parse_timespan,
)
from mediahub.integrations.base import InstanceConfig
from mediahub.models.common import ServiceType, SourceRef
from mediahub.models.downloads import ActivityKind, DownloadItem, DownloadState

FIXTURES = Path(__file__).parent.parent / "fixtures"
GB = 1024**3


def fixture(app: str, name: str = "constructed") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / app / f"{name}.json").read_text())
    return data


def source(st: ServiceType) -> SourceRef:
    return SourceRef(
        server_id="s", server_name="S", instance_id="arr-1", instance_name="Arr", service_type=st
    )


def fake_url(media_id: str, tag: str) -> str:
    return f"img:{media_id}:{tag}"


def queue(app: str) -> dict[str, DownloadItem]:
    st = ServiceType(app)
    items = map_queue(fixture(app)["queue"]["records"], source(st), fake_url)
    return {d.release_title or d.title: d for d in items}


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("00:12:34", 754),
        ("1.02:03:04", 93784),
        ("01:19:12.4310000", 4752.431),
        ("-00:00:05", -5),
        ("garbage", None),
        (None, None),
    ],
)
def test_parse_timespan(value: object, seconds: float | None) -> None:
    assert parse_timespan(value) == (pytest.approx(seconds) if seconds is not None else None)


@pytest.mark.parametrize(
    ("record", "state"),
    [
        ({"status": "downloading", "trackedDownloadState": "downloading"}, "downloading"),
        ({"status": "warning", "trackedDownloadState": "downloading"}, "stalled"),
        ({"status": "paused", "trackedDownloadState": "downloading"}, "waiting"),
        ({"status": "delay"}, "waiting"),
        ({"status": "downloadClientUnavailable"}, "waiting"),
        ({"status": "completed", "trackedDownloadState": "importPending"}, "awaiting_import"),
        ({"status": "completed", "trackedDownloadState": "importing"}, "importing"),
        (
            {
                "status": "completed",
                "trackedDownloadState": "importBlocked",
                "trackedDownloadStatus": "warning",
            },
            "import_blocked",
        ),
        # Single-file rejection and path problems never reach importBlocked; retried.
        (
            {
                "status": "completed",
                "trackedDownloadState": "importPending",
                "trackedDownloadStatus": "warning",
            },
            "import_warning",
        ),
        (
            {
                "status": "completed",
                "trackedDownloadState": "downloading",
                "trackedDownloadStatus": "warning",
            },
            "import_warning",
        ),
        ({"status": "failed", "trackedDownloadState": "failedPending"}, "failed"),
        (
            {
                "status": "failed",
                "trackedDownloadState": "downloading",
                "trackedDownloadStatus": "warning",
            },
            "failed",
        ),
        (
            {
                "status": "completed",
                "trackedDownloadState": "failedPending",
                "trackedDownloadStatus": "error",
            },
            "failed",
        ),
        ({"status": "unknown"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_map_state(record: dict[str, Any], state: str) -> None:
    assert map_state(record) is DownloadState(state)


def test_episode_label() -> None:
    ep = lambda s, e: {"seasonNumber": s, "episodeNumber": e}  # noqa: E731
    assert episode_label([ep(2, 3)]) == "S02E03"
    assert episode_label([ep(2, 3), ep(2, 1), ep(2, 8)]) == "S02E01–E08"
    assert episode_label([ep(1, 1), ep(3, 2)]) == "S01–S03"
    assert episode_label([{"seasonNumber": 1}]) is None


def test_sonarr_downloading_item() -> None:
    d = queue("sonarr")["Release 1"]
    assert d.state is DownloadState.DOWNLOADING
    assert (d.title, d.subtitle) == ("Series 1", "S01E01 · Episode 1")
    assert d.image_url == "img:1:poster-250"
    assert (d.client, d.protocol, d.indexer, d.quality) == (
        "SABnzbd",
        "usenet",
        "Indexer 1 (Prowlarr)",
        "WEBDL-1080p",
    )
    assert d.size_bytes == 2 * GB and d.remaining_bytes == GB and d.progress_pct == 50
    assert d.eta_s == 754
    assert d.added_at == datetime(2026, 10, 24, 11, tzinfo=UTC)
    assert (d.raw_status, d.raw_tracked_state, d.raw_tracked_status) == (
        "downloading",
        "downloading",
        "ok",
    )


def test_sonarr_stalled_torrent_keeps_client_message() -> None:
    d = queue("sonarr")["Release 2"]
    assert d.state is DownloadState.STALLED
    assert d.error_message == "The download is stalled with no connections"


def test_sonarr_season_pack_is_grouped() -> None:
    items = map_queue(fixture("sonarr")["queue"]["records"], source(ServiceType.SONARR), fake_url)
    pack = [d for d in items if d.release_title == "Release 5"]
    assert len(pack) == 1
    d = pack[0]
    assert d.episode_count == 3 and d.subtitle == "S02E01–E03"
    assert d.state is DownloadState.IMPORT_BLOCKED
    assert d.size_bytes == 12 * GB  # the download's size, not summed per episode
    assert d.messages[1].messages == [
        "Not an upgrade for existing episode file(s). Existing quality: WEBDL-1080p. "
        "New Quality WEBDL-1080p."
    ]


def test_sonarr_single_file_rejection_is_a_warning() -> None:
    d = queue("sonarr")["Release 6"]
    assert d.state is DownloadState.IMPORT_WARNING and d.raw_tracked_state == "importPending"


def test_real_radarr_downloading() -> None:
    """Captured from Radarr 6.4.4 + SABnzbd."""
    [d] = map_queue(
        fixture("radarr", "downloading")["queue"]["records"], source(ServiceType.RADARR), fake_url
    )
    assert d.state is DownloadState.DOWNLOADING
    assert (d.title, d.subtitle, d.quality, d.client) == (
        "Movie 1",
        "2026",
        "Bluray-1080p",
        "SABnzbd",
    )
    assert d.progress_pct == pytest.approx(6.08, abs=0.01)
    assert d.eta_s == 249
    assert d.error_message is None and d.messages == []


def test_real_radarr_import_warning_during_post_processing() -> None:
    """Captured: completed + importPending + warning while FileFlows converted the download;
    imported by itself 3 minutes later."""
    [d] = map_queue(
        fixture("radarr", "import-warning")["queue"]["records"],
        source(ServiceType.RADARR),
        fake_url,
    )
    assert d.state is DownloadState.IMPORT_WARNING
    assert d.error_message is None  # Radarr sent errorMessage: ""
    assert d.messages[0].messages == ["No files found are eligible for import in /redacted/file"]
    assert d.progress_pct == 100


def test_real_radarr_history_with_retried_failures() -> None:
    """Captured: two failed grabs (SABnzbd aborted) before a third release imported."""
    events = map_history(
        fixture("radarr", "history")["history"]["records"], source(ServiceType.RADARR), fake_url
    )
    assert [e.kind for e in events[:6]] == [
        ActivityKind.IMPORTED,
        ActivityKind.GRABBED,
        ActivityKind.FAILED,
        ActivityKind.GRABBED,
        ActivityKind.FAILED,
        ActivityKind.GRABBED,
    ]
    assert events[2].detail == "Aborted, cannot be completed - https://sabnzbd.org/not-complete"
    assert all(e.title.startswith("Movie ") and e.image_url for e in events)


def test_real_sonarr_history() -> None:
    """Captured from Sonarr 4.0.20: upgrade imports paired with file deletions."""
    events = map_history(
        fixture("sonarr", "history")["history"]["records"], source(ServiceType.SONARR), fake_url
    )
    assert events[0].kind is ActivityKind.IMPORTED and events[1].kind is ActivityKind.DELETED
    assert events[1].detail in ("Reason: Upgrade", "Reason: Manual")
    assert all(e.subtitle and e.subtitle.startswith("S") for e in events)
    assert ActivityKind.OTHER not in {e.kind for e in events}


def test_sonarr_pending_release_has_no_download_id() -> None:
    d = queue("sonarr")["Release 9"]
    assert d.state is DownloadState.WAITING and d.raw_status == "delay"
    assert d.download_id is None and d.raw_tracked_state is None and d.client is None
    assert d.item_id == "99120411"


def test_sonarr_unknown_series_uses_release_title() -> None:
    d = queue("sonarr")["Release 10"]
    assert d.title == "Release 10" and d.release_title is None and d.subtitle is None
    assert d.image_url is None
    assert d.state is DownloadState.IMPORT_BLOCKED


def test_radarr_queue() -> None:
    items = queue("radarr")
    blocked = items["Release 2"]
    assert (blocked.title, blocked.subtitle) == ("Movie 2", "2022")
    assert blocked.state is DownloadState.IMPORT_BLOCKED
    assert blocked.image_url == "img:2:poster-250"
    assert items["Release 3"].state is DownloadState.IMPORTING
    assert items["Release 4"].state is DownloadState.WAITING
    assert items["Release 5"].state is DownloadState.FAILED


def test_malformed_records_are_skipped() -> None:
    assert map_queue([None, "x", {"title": "no id"}], source(ServiceType.RADARR), fake_url) == []
    assert map_queue({"records": []}, source(ServiceType.RADARR), fake_url) == []


def test_sonarr_history() -> None:
    events = map_history(
        fixture("sonarr")["history"]["records"], source(ServiceType.SONARR), fake_url
    )
    by_id = {e.event_id: e for e in events}
    assert len(events) == 8  # the season pack's three grab rows collapse into one
    grab = by_id["9001"]
    assert grab.kind is ActivityKind.GRABBED
    assert (grab.title, grab.subtitle) == ("Series 1", "S01E01 · Episode 1")
    assert grab.detail == "WEBDL-1080p · Indexer 1 (Prowlarr)"
    assert by_id["9011"].subtitle == "S02E01–E03 · 3 episodes"
    assert by_id["9020"].kind is ActivityKind.IMPORTED
    assert by_id["9021"].kind is ActivityKind.FAILED
    assert by_id["9021"].detail == "Unpacking failed, write error or disk is full?"
    assert by_id["9022"].detail == "Reason: Upgrade"
    assert by_id["9023"].kind is ActivityKind.RENAMED
    assert by_id["9024"].kind is ActivityKind.IGNORED
    assert by_id["9025"].kind is ActivityKind.OTHER


def test_radarr_history() -> None:
    events = map_history(
        fixture("radarr")["history"]["records"], source(ServiceType.RADARR), fake_url
    )
    kinds = [e.kind for e in events]
    assert kinds == [
        ActivityKind.GRABBED,
        ActivityKind.IMPORTED,
        ActivityKind.FAILED,
        ActivityKind.DELETED,
        ActivityKind.RENAMED,
        ActivityKind.IGNORED,
    ]
    assert (events[2].title, events[2].subtitle) == ("Movie 7", "2027")
    assert events[2].detail == "Failed download detected"


def _integration(cls: type[ArrIntegration], st: ServiceType) -> ArrIntegration:
    cfg = InstanceConfig(
        id="arr-1",
        service_type=st,
        name="Arr",
        base_url="http://arr:8989",
        secrets={"api_key": "K"},
    )
    return cls(cfg, source(st))


def _mock_app(app: str) -> dict[str, respx.Route]:
    data = fixture(app)
    return {
        "status": respx.get("http://arr:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json=data["system_status"])
        ),
        "health": respx.get("http://arr:8989/api/v3/health").mock(
            return_value=httpx.Response(200, json=data["health"])
        ),
        "queue": respx.get("http://arr:8989/api/v3/queue").mock(
            return_value=httpx.Response(200, json=data["queue"])
        ),
        "history": respx.get("http://arr:8989/api/v3/history").mock(
            return_value=httpx.Response(200, json=data["history"])
        ),
    }


@respx.mock
async def test_sonarr_poll_full_and_idle_both_fetch_history() -> None:
    routes = _mock_app("sonarr")
    sonarr = _integration(SonarrIntegration, ServiceType.SONARR)

    full = await sonarr.poll(full=True)
    assert full.version == "4.0.20.3014"
    assert full.downloads is not None and len(full.downloads) == 10
    assert full.history is not None and len(full.history) == 8
    params = routes["queue"].calls.last.request.url.params
    assert params["includeEpisode"] == "true" and params["includeUnknownSeriesItems"] == "true"
    assert params["pageSize"] == "500"
    assert routes["queue"].calls.last.request.headers["X-Api-Key"] == "K"

    idle = await sonarr.poll(full=False)
    assert idle.downloads is not None and idle.history is not None
    assert routes["history"].call_count == 2


@respx.mock
async def test_radarr_poll_uses_movie_includes() -> None:
    routes = _mock_app("radarr")
    result = await _integration(RadarrIntegration, ServiceType.RADARR).poll(full=True)
    assert result.downloads is not None and len(result.downloads) == 5
    assert routes["queue"].calls.last.request.url.params["includeMovie"] == "true"
    assert routes["history"].calls.last.request.url.params["includeMovie"] == "true"


@respx.mock
async def test_unexpected_queue_shape_is_a_protocol_error() -> None:
    _mock_app("radarr")
    respx.get("http://arr:8989/api/v3/queue").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(Exception, match="Unexpected /api/v3/queue response"):
        await _integration(RadarrIntegration, ServiceType.RADARR).poll(full=False)


@respx.mock
async def test_poster_fetch_uses_api_mediacover() -> None:
    route = respx.get("http://arr:8989/api/v3/mediacover/12/poster-250.jpg").mock(
        return_value=httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})
    )
    sonarr = _integration(SonarrIntegration, ServiceType.SONARR)
    assert await sonarr.fetch_image("12/poster-250") == (b"jpg", "image/jpeg")
    assert route.calls.last.request.headers["X-Api-Key"] == "K"
    assert await sonarr.fetch_image("12/fanart") is None
    assert await sonarr.fetch_image("../x/poster-250") is None


def test_poster_urls_are_signed_for_the_instance() -> None:
    sonarr = _integration(SonarrIntegration, ServiceType.SONARR)
    url = sonarr._image_url("12", "poster-250")
    assert url is not None
    _, _, instance, item, tag, sig = url.split("/")
    assert (instance, item, tag) == ("arr-1", "12", "poster-250")
    assert image_refs.verify(instance, item, tag, sig)


@pytest.mark.parametrize(
    ("value", "stored", "error"),
    [
        ("30", 30, False),
        ("0", 0, False),
        ("", None, False),
        ("abc", None, True),
        ("9999", None, True),
    ],
)
def test_import_warning_delay_option(value: str, stored: int | None, error: bool) -> None:
    form = parse_instance_form(
        {
            "service_type": "radarr",
            "name": "Radarr",
            "base_url": "http://radarr:7878",
            "secret_api_key": "K",
            "option_import_warning_grace_min": value,
        }
    )
    assert form.options.get("import_warning_grace_min") == stored
    assert ("option_import_warning_grace_min" in form.errors) is error


@respx.mock
async def test_poll_reports_configured_import_warning_delay() -> None:
    _mock_app("radarr")
    cfg = InstanceConfig(
        id="arr-1",
        service_type=ServiceType.RADARR,
        name="Arr",
        base_url="http://arr:8989",
        secrets={"api_key": "K"},
        options={"import_warning_grace_min": 45},
    )
    integration = RadarrIntegration(cfg, source(ServiceType.RADARR))
    assert (await integration.poll(full=False)).import_warning_grace_s == 2700
    default = _integration(RadarrIntegration, ServiceType.RADARR)
    assert (await default.poll(full=False)).import_warning_grace_s is None
