"""Next-episode health check: Emby lookup, InfiniDysk recheck call and the poll target."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from mediahub.db.app_settings import ServerIdentity
from mediahub.instances.forms import parse_instance_form
from mediahub.integrations.base import InstanceConfig, Integration
from mediahub.integrations.demo import DemoIntegration, demo_local_configs
from mediahub.integrations.emby.integration import EmbyIntegration, NextEpisode
from mediahub.integrations.emby.mapper import map_session
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import request
from mediahub.integrations.infinidysk.integration import (
    RECHECK_PATH,
    ActiveCheck,
    CheckResult,
    InfiniDyskIntegration,
    RecheckOutcome,
    assert_read_only,
)
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.playback import (
    MediaInfo,
    MediaKind,
    NextCheckState,
    NextEpisodeCheck,
    PlaybackSession,
)
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.next_episode import (
    FOLLOW_S,
    RETRY_S,
    NextEpisodeTarget,
    dav_item_id,
    finished_state,
)
from mediahub.monitoring.store import SnapshotStore

EMBY = "http://emby:8096/emby"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "emby"


def captured() -> dict[str, Any]:
    """Real Emby 4.11 capture: S01E01 of a 36-episode STRM series playing at 57%, with the
    series' /Shows/{id}/Episodes list. The live run asked InfiniDysk to check S01E02."""
    data: dict[str, Any] = json.loads((FIXTURES / "next-episode.json").read_text())
    return data


def _source(instance_id: str, service_type: ServiceType) -> SourceRef:
    return SourceRef(
        server_id="s",
        server_name="S",
        instance_id=instance_id,
        instance_name=instance_id,
        service_type=service_type,
    )


def emby(options: dict[str, Any] | None = None, instance_id: str = "emby-1") -> EmbyIntegration:
    cfg = InstanceConfig(
        id=instance_id,
        service_type=ServiceType.EMBY,
        name="Emby",
        base_url="http://emby:8096",
        secrets={"api_key": "K"},
        options=options or {},
    )
    return EmbyIntegration(cfg, _source(instance_id, ServiceType.EMBY))


def episode(item_id: str, season: int, number: int, **extra: Any) -> dict[str, Any]:
    """An item shaped like the captured /Shows/{id}/Episodes ones, for orders the capture
    doesn't have (specials, missing episodes, season boundaries)."""
    return {
        "Id": item_id,
        "Name": f"Episode {item_id}",
        "ParentIndexNumber": season,
        "IndexNumber": number,
        "SeriesId": "900",
        "SeasonId": f"{900 + season}",
        "Type": "Episode",
        "MediaType": "Video",
        "IsFolder": False,
        **extra,
    }


def mock_episodes(*items: dict[str, Any]) -> respx.Route:
    return respx.get(f"{EMBY}/Shows/900/Episodes").mock(
        return_value=httpx.Response(
            200, json={"Items": list(items), "TotalRecordCount": len(items)}
        )
    )


def mock_strm(item_id: str, path: str) -> respx.Route:
    return respx.get(f"{EMBY}/Users/u1/Items/{item_id}").mock(
        return_value=httpx.Response(200, json={"MediaSources": [{"Id": "m", "Path": path}]})
    )


STRM = "http://idk:3000/view/.ids/4/0/c/{}?downloadKey=SECRET&extension=mkv"


# ----- Emby: the next episode -----------------------------------------------------------


@respx.mock
async def test_next_episode_from_the_captured_episode_list() -> None:
    capture = captured()
    raw = capture["sessions"][0]
    item, user = raw["NowPlayingItem"], raw["UserId"]
    listing = capture["episodes"][item["SeriesId"]]
    assert listing["Items"][0]["Id"] == item["Id"]  # S01E01 plays
    nxt = listing["Items"][1]
    route = respx.get(f"{EMBY}/Shows/{item['SeriesId']}/Episodes").mock(
        return_value=httpx.Response(200, json=listing)
    )
    respx.get(f"{EMBY}/Users/{user}/Items/{nxt['Id']}").mock(
        return_value=httpx.Response(
            200, json={"MediaSources": [{"Id": "m", "Path": STRM.format(DAV_ID)}]}
        )
    )
    integration = emby()
    try:
        found = await integration.next_episode(user, item["SeriesId"], item["Id"])
    finally:
        await integration.aclose()
    assert found == NextEpisode(
        item_id=nxt["Id"], code="S01E02", name=nxt["Name"], strm_id=str(DAV_ID)
    )
    assert route.calls.last.request.url.params["UserId"] == user


@respx.mock
async def test_next_episode_in_the_same_season_with_its_strm_target() -> None:
    route = mock_episodes(
        episode("100", 0, 1), episode("101", 1, 1), episode("102", 1, 2), episode("201", 2, 1)
    )
    mock_strm("102", STRM.format("ab-102"))
    integration = emby()
    try:
        found = await integration.next_episode("u1", "900", "101")
    finally:
        await integration.aclose()
    assert found == NextEpisode(item_id="102", code="S01E02", name="Episode 102", strm_id="ab-102")
    params = route.calls.last.request.url.params
    assert params["UserId"] == "u1" and params["IsMissing"] == "false"


@respx.mock
async def test_next_episode_crosses_seasons_and_skips_missing_and_specials() -> None:
    mock_episodes(
        episode("101", 1, 1),
        episode("102", 1, 2),
        episode("103", 1, 3, LocationType="Virtual"),  # missing, shown by Emby
        episode("150", 0, 1),  # a special placed inside the season
        episode("201", 2, 1),
    )
    mock_strm("201", STRM.format("ab-201"))
    integration = emby()
    try:
        found = await integration.next_episode("u1", "900", "102")
    finally:
        await integration.aclose()
    assert found is not None and (found.item_id, found.code) == ("201", "S02E01")


@respx.mock
async def test_a_playing_special_continues_with_the_next_item() -> None:
    mock_episodes(episode("100", 0, 1), episode("101", 1, 1))
    mock_strm("101", STRM.format("ab-101"))
    integration = emby()
    try:
        found = await integration.next_episode("u1", "900", "100")
    finally:
        await integration.aclose()
    assert found is not None and found.code == "S01E01"


@respx.mock
async def test_no_next_episode_and_unknown_item() -> None:
    mock_episodes(episode("101", 1, 1), episode("102", 1, 2))
    integration = emby()
    try:
        assert await integration.next_episode("u1", "900", "102") is None  # last one
        assert await integration.next_episode("u1", "900", "999") is None  # not in the list
    finally:
        await integration.aclose()


@respx.mock
async def test_next_episode_without_a_strm_file() -> None:
    mock_episodes(episode("101", 1, 1), episode("102", 1, 2))
    mock_strm("102", "/media/tv/Show/Season 01/Show - S01E02.mkv")
    integration = emby()
    try:
        found = await integration.next_episode("u1", "900", "101")
    finally:
        await integration.aclose()
    assert found is not None and found.strm_id is None


@respx.mock
async def test_next_episode_rejects_bad_ids_and_bad_responses() -> None:
    route = respx.get(f"{EMBY}/Shows/900/Episodes").mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )
    integration = emby()
    try:
        assert await integration.next_episode("u1", "../x", "101") is None
        assert not route.called
        with pytest.raises(IntegrationError):
            await integration.next_episode("u1", "900", "101")
    finally:
        await integration.aclose()


# ----- InfiniDysk: the recheck request --------------------------------------------------

IDK = "http://idk:3000"
DAV_ID = UUID("40c6bd4c-971e-4d2a-9a51-3f1f1b2c3d4e")


def infinidysk(instance_id: str = "idk-1") -> InfiniDyskIntegration:
    cfg = InstanceConfig(
        id=instance_id,
        service_type=ServiceType.INFINIDYSK,
        name="InfiniDysk",
        base_url=IDK,
        secrets={"api_key": "K"},
    )
    return InfiniDyskIntegration(cfg, _source(instance_id, ServiceType.INFINIDYSK))


@respx.mock
async def test_recheck_sends_one_plain_form_field() -> None:
    route = respx.post(f"{IDK}/api/recheck-file").mock(
        return_value=httpx.Response(
            202, json={"status": True, "davItemId": str(DAV_ID), "state": "queued"}
        )
    )
    integration = infinidysk()
    try:
        outcome = await integration.recheck_file(DAV_ID)
    finally:
        await integration.aclose()
    assert outcome == RecheckOutcome(state="queued")
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "K"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content.decode()
    assert body.count("Content-Disposition") == 1
    assert 'name="davItemId"' in body and "filename" not in body
    assert str(DAV_ID) in body


@pytest.mark.parametrize("state", ["already-queued", "already-running"])
@respx.mock
async def test_recheck_reports_existing_work(state: str) -> None:
    respx.post(f"{IDK}/api/recheck-file").mock(
        return_value=httpx.Response(202, json={"davItemId": str(DAV_ID), "state": state})
    )
    integration = infinidysk()
    try:
        assert (await integration.recheck_file(DAV_ID)).state == state
    finally:
        await integration.aclose()


@pytest.mark.parametrize(
    ("status", "error", "not_found"),
    [
        (404, "File no longer exists.", True),
        (409, "This file type does not support a health recheck.", False),
        (409, "Background repairs are disabled.", False),
    ],
)
@respx.mock
async def test_recheck_refusals_come_back_verbatim(
    status: int, error: str, not_found: bool
) -> None:
    respx.post(f"{IDK}/api/recheck-file").mock(
        return_value=httpx.Response(status, json={"status": False, "error": error})
    )
    integration = infinidysk()
    try:
        outcome = await integration.recheck_file(DAV_ID)
    finally:
        await integration.aclose()
    assert outcome == RecheckOutcome(state=None, error=error, not_found=not_found)


@respx.mock
async def test_recheck_errors_are_translated() -> None:
    route = respx.post(f"{IDK}/api/recheck-file")
    integration = infinidysk()
    try:
        route.mock(return_value=httpx.Response(404, text="<html>nope</html>"))
        with pytest.raises(IntegrationError, match="check the base URL"):
            await integration.recheck_file(DAV_ID)
        route.mock(return_value=httpx.Response(401))
        with pytest.raises(IntegrationError) as exc:
            await integration.recheck_file(DAV_ID)
        assert exc.value.kind is ErrorKind.AUTH
        route.mock(side_effect=httpx.ConnectError("Connection refused"))
        with pytest.raises(IntegrationError) as exc:
            await integration.recheck_file(DAV_ID)
        assert exc.value.kind is ErrorKind.UNREACHABLE
    finally:
        await integration.aclose()


def test_recheck_path_stays_off_the_read_allowlist() -> None:
    with pytest.raises(IntegrationError, match="non-allowlisted"):
        assert_read_only(RECHECK_PATH, None, backend=False)


@respx.mock
async def test_request_returns_accepted_error_statuses() -> None:
    respx.get("http://h/x").mock(return_value=httpx.Response(409, json={"error": "e"}))
    async with httpx.AsyncClient(base_url="http://h") as client:
        assert (await request(client, "GET", "/x", accept=(409,))).status_code == 409
        with pytest.raises(IntegrationError):
            await request(client, "GET", "/x")


# ----- Settings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "value", "stored", "error", "percent"),
    [
        ("on", "70", 70, False, 70),
        ("on", "0", 0, False, 0),
        ("on", "", None, False, 50),
        (None, "70", 70, False, None),  # off: the percentage is kept but unused
        ("on", "96", None, True, 50),
        ("on", "half", None, True, 50),
    ],
)
async def test_next_episode_options(
    check: str | None, value: str, stored: int | None, error: bool, percent: int | None
) -> None:
    data = {
        "service_type": "emby",
        "name": "Emby",
        "base_url": "http://emby:8096",
        "secret_api_key": "K",
        "option_next_episode_percent": value,
    }
    if check:
        data["option_next_episode_check"] = check
    form = parse_instance_form(data)
    assert form.options["next_episode_check"] is (check == "on")
    assert form.options.get("next_episode_percent") == stored
    assert ("option_next_episode_percent" in form.errors) is error
    integration = emby(form.options)
    try:
        assert integration.next_episode_percent == percent
    finally:
        await integration.aclose()


# ----- The poll target ------------------------------------------------------------------

ON = {"next_episode_check": True}


def playing(item_id: str = "101", progress: float = 60, session_id: str = "s1") -> PlaybackSession:
    return PlaybackSession(
        source=_source("emby-1", ServiceType.EMBY),
        session_id=session_id,
        item_id=item_id,
        user_id="u1",
        series_id="900",
        position_s=progress * 10,
        duration_s=1000,
        media=MediaInfo(title="Series 1", kind=MediaKind.EPISODE),
    )


class Harness:
    def __init__(self, integrations: list[Integration]) -> None:
        self.store = SnapshotStore(identity=ServerIdentity(server_id="s", server_name="S"))
        self.integrations = integrations
        self.now = 1000.0
        self.wall = ASKED_AT
        self.target = NextEpisodeTarget(
            self.store, lambda: self.integrations, lambda: self.now, lambda: self.wall
        )

    def play(self, *sessions: PlaybackSession) -> None:
        self.store.put_local(
            ServiceSnapshot(
                source=_source("emby-1", ServiceType.EMBY),
                status=ServiceStatus.OK,
                sessions=list(sessions),
            )
        )

    async def run(self) -> None:
        assert await self.target.run_once(active=False)

    def check(self, item_id: str = "101", session_id: str = "s1") -> NextEpisodeCheck | None:
        return self.store.next_check(("emby-1", session_id, item_id))

    async def aclose(self) -> None:
        for integration in self.integrations:
            await integration.aclose()


ASKED_AT = datetime(2026, 10, 28, 22, 26, 49, tzinfo=UTC)


def active_item(progress: int = 40, phase: str = "Checking", dav: UUID = DAV_ID) -> dict[str, Any]:
    """An /api/get-active-health-checks item (InfiniDysk's HealthCheckQueueItem)."""
    return {
        "id": str(dav),
        "name": "file.mkv",
        "path": "/content/tv/file.mkv",
        "releaseDate": None,
        "lastHealthCheck": None,
        "nextHealthCheck": "1970-01-01T00:00:01+00:00",
        "countsTowardUncheckedCount": False,
        "progress": progress,
        "phase": phase,
        "phaseStartedAt": ASKED_AT.isoformat(),
    }


def history_item(
    at: datetime,
    result: int = 0,
    repair: int = 0,
    message: str = "File is healthy.",
    dav: UUID = DAV_ID,
) -> dict[str, Any]:
    """An /api/get-health-check-history item, as seen live."""
    return {
        "id": str(uuid4()),
        "createdAt": at.isoformat(),
        "davItemId": str(dav),
        "path": "/content/tv/file.mkv",
        "nzbFileName": None,
        "jobName": None,
        "result": result,
        "repairStatus": repair,
        "message": message,
    }


def idk_reads(base: str = IDK) -> tuple[respx.Route, respx.Route]:
    """InfiniDysk's active checks and history, empty until a test fills them."""
    active = respx.get(f"{base}/api/get-active-health-checks").mock(
        return_value=httpx.Response(200, json={"status": True, "items": []})
    )
    history = respx.get(f"{base}/api/get-health-check-history").mock(
        return_value=httpx.Response(200, json={"status": True, "items": [], "totalCount": 0})
    )
    return active, history


def reads(route: respx.Route, *items: dict[str, Any]) -> None:
    route.mock(return_value=httpx.Response(200, json={"status": True, "items": list(items)}))


def series(*extra: dict[str, Any]) -> tuple[respx.Route, respx.Route]:
    mock_episodes(episode("101", 1, 1), episode("102", 1, 2), episode("201", 2, 1), *extra)
    mock_strm("102", STRM.format(f"{DAV_ID}"))
    mock_strm("201", STRM.format("11111111-2222-3333-4444-555555555555.mkv"))
    return idk_reads()


def accepted(state: str = "queued") -> httpx.Response:
    return httpx.Response(202, json={"davItemId": str(DAV_ID), "state": state})


def test_dav_item_id() -> None:
    assert dav_item_id(str(DAV_ID)) == DAV_ID
    assert dav_item_id(f"{DAV_ID}.mkv") == DAV_ID
    assert dav_item_id("Show - S01E02.mkv") is None and dav_item_id(None) is None


@respx.mock
async def test_checks_the_next_episode_once_past_the_threshold() -> None:
    series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing(progress=49))
        await h.run()
        assert not recheck.called and h.check() is None
        h.play(playing(progress=50))
        await h.run()
        await h.run()
    finally:
        await h.aclose()
    assert recheck.call_count == 1
    assert str(DAV_ID) in recheck.calls.last.request.content.decode()
    assert h.check() == NextEpisodeCheck(
        state=NextCheckState.QUEUED, code="S01E02", name="Episode 102"
    )


@respx.mock
async def test_nothing_happens_with_the_option_off_or_without_infinidysk() -> None:
    respx.route().mock(side_effect=AssertionError("no request expected"))
    for integrations in ([emby(), infinidysk()], [emby(ON)]):
        h = Harness(integrations)
        try:
            h.play(playing(progress=90))
            await h.run()
        finally:
            await h.aclose()
        assert h.check() is None


@respx.mock
async def test_demo_integrations_are_ignored() -> None:
    respx.route().mock(side_effect=AssertionError("no request expected"))
    config = next(c for c in demo_local_configs() if c.service_type is ServiceType.EMBY)
    demo = DemoIntegration(config, _source(config.id, ServiceType.EMBY))
    h = Harness([demo, infinidysk()])
    try:
        h.play(playing(progress=90))
        await h.run()
    finally:
        await h.aclose()
    assert h.check() is None


@respx.mock
async def test_autoplay_into_the_next_episode_checks_again() -> None:
    series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing("101"))
        await h.run()
        h.play(playing("102"))  # same session, next item
        await h.run()
    finally:
        await h.aclose()
    assert recheck.call_count == 2
    check = h.check("102")
    assert check is not None and check.code == "S02E01"
    assert h.check("101") is None  # that item no longer plays


@respx.mock
async def test_a_file_asked_for_in_the_last_day_is_not_asked_again() -> None:
    series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing(session_id="s1"))
        await h.run()
        h.play(playing(session_id="s2"))  # watched again on another device
        await h.run()
        recent = h.check(session_id="s2")
        h.now += 24 * 3600
        h.play(playing(session_id="s3"))
        await h.run()
    finally:
        await h.aclose()
    assert recent is not None and recent.state is NextCheckState.RECENT
    assert recheck.call_count == 2


@respx.mock
async def test_unreachable_infinidysk_is_retried_later() -> None:
    series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        failed = h.check()
        await h.run()
        assert recheck.call_count == 1
        recheck.mock(return_value=accepted("already-queued"))
        h.now += RETRY_S
        await h.run()
    finally:
        await h.aclose()
    assert failed is not None and failed.state is NextCheckState.ERROR
    assert failed.message == "Connection refused"
    check = h.check()
    assert check is not None and check.state is NextCheckState.ALREADY_QUEUED


@respx.mock
async def test_the_infinidysk_that_has_the_file_answers() -> None:
    series()
    other = respx.post("http://idk2:3000/api/recheck-file").mock(
        return_value=httpx.Response(404, json={"status": False, "error": "File no longer exists."})
    )
    mine = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    idk_reads("http://idk2:3000")
    second = InfiniDyskIntegration(
        InstanceConfig(
            id="idk-2",
            service_type=ServiceType.INFINIDYSK,
            name="Other",
            base_url="http://idk2:3000",
            secrets={"api_key": "K"},
        ),
        _source("idk-2", ServiceType.INFINIDYSK),
    )
    h = Harness([emby(ON), second, infinidysk()])
    try:
        h.play(playing())
        await h.run()
    finally:
        await h.aclose()
    assert other.called and mine.called
    check = h.check()
    assert check is not None and check.state is NextCheckState.QUEUED


@pytest.mark.parametrize(
    ("status", "error", "state"),
    [
        (404, "File no longer exists.", NextCheckState.NOT_FOUND),
        (409, "Background repairs are disabled.", NextCheckState.REFUSED),
    ],
)
@respx.mock
async def test_refusals_are_shown_verbatim_and_not_retried(
    status: int, error: str, state: NextCheckState
) -> None:
    series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(
        return_value=httpx.Response(status, json={"status": False, "error": error})
    )
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        h.now += RETRY_S
        await h.run()
    finally:
        await h.aclose()
    assert recheck.call_count == 1
    assert h.check() == NextEpisodeCheck(
        state=state, code="S01E02", name="Episode 102", message=error
    )


@respx.mock
async def test_last_episode_and_episode_without_infinidysk_file() -> None:
    mock_episodes(episode("101", 1, 1), episode("102", 1, 2))
    mock_strm("102", "/media/tv/Show - S01E02.mkv")
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing("101", session_id="a"), playing("102", session_id="b"))
        await h.run()
    finally:
        await h.aclose()
    assert not recheck.called
    no_file, last = h.check("101", "a"), h.check("102", "b")
    assert no_file is not None and no_file.state is NextCheckState.NO_FILE
    assert last == NextEpisodeCheck(state=NextCheckState.NO_NEXT)


@respx.mock
async def test_outcomes_are_forgotten_when_playback_ends() -> None:
    series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        assert h.check() is not None
        h.play()
        await h.run()
    finally:
        await h.aclose()
    assert h.check() is None


@respx.mock
async def test_captured_session_triggers_one_check_of_the_next_episode() -> None:
    capture = captured()
    raw = capture["sessions"][0]
    item, user = raw["NowPlayingItem"], raw["UserId"]
    listing = capture["episodes"][item["SeriesId"]]
    respx.get(f"{EMBY}/Shows/{item['SeriesId']}/Episodes").mock(
        return_value=httpx.Response(200, json=listing)
    )
    respx.get(f"{EMBY}/Users/{user}/Items/{listing['Items'][1]['Id']}").mock(
        return_value=httpx.Response(
            200, json={"MediaSources": [{"Id": "m", "Path": STRM.format(DAV_ID)}]}
        )
    )
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    idk_reads()
    session = map_session(raw, _source("emby-1", ServiceType.EMBY), lambda i, t: None)
    assert session is not None and session.progress_pct is not None
    assert session.progress_pct > 50
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(session)
        await h.run()
        await h.run()
    finally:
        await h.aclose()
    assert recheck.call_count == 1
    check = h.check(item["Id"], session.session_id)
    assert check is not None and (check.state, check.code) == (NextCheckState.QUEUED, "S01E02")


# ----- Following the check to its result --------------------------------------------------


@pytest.mark.parametrize(
    ("result", "repair", "state"),
    [
        (0, 0, NextCheckState.HEALTHY),
        (2, 0, NextCheckState.DEGRADED),
        (1, 0, NextCheckState.UNHEALTHY),
        (1, 1, NextCheckState.REPAIRED),
        (1, 2, NextCheckState.DELETED),
        (1, 3, NextCheckState.ACTION_NEEDED),
        (1, 4, NextCheckState.REPAIRED_PAR2),
    ],
)
def test_finished_state(result: int, repair: int, state: NextCheckState) -> None:
    check = CheckResult(dav_item_id=DAV_ID, at=ASKED_AT, result=result, repair=repair, message=None)
    assert finished_state(check) is state


@respx.mock
async def test_active_checks_and_history_are_read_tolerantly() -> None:
    active, history = idk_reads()
    reads(active, active_item(55, "Repairing"), {"id": "not-a-guid"}, "junk")
    reads(
        history,
        history_item(ASKED_AT, 1, 4, "Repaired from PAR2."),
        {**history_item(ASKED_AT), "createdAt": "yesterday"},
        {**history_item(ASKED_AT), "result": None},
    )
    integration = infinidysk()
    try:
        running = await integration.active_checks()
        results = await integration.check_results()
    finally:
        await integration.aclose()
    assert running == {DAV_ID: ActiveCheck(phase="Repairing", progress=55)}
    assert results == [
        CheckResult(
            dav_item_id=DAV_ID, at=ASKED_AT, result=1, repair=4, message="Repaired from PAR2."
        )
    ]
    assert history.calls.last.request.url.params["pageSize"] == "100"
    assert history.calls.last.request.headers["x-api-key"] == "K"


@respx.mock
async def test_card_follows_the_check_until_it_finishes() -> None:
    active, history = series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    earlier = history_item(ASKED_AT - timedelta(seconds=27))  # an older check of the same file
    reads(active, active_item(40))
    reads(history, earlier)
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        checking = h.check()
        reads(active)
        reads(history, history_item(ASKED_AT + timedelta(seconds=35)), earlier)
        await h.run()
        healthy = h.check()
        await h.run()
    finally:
        await h.aclose()
    assert checking == NextEpisodeCheck(
        state=NextCheckState.CHECKING, code="S01E02", name="Episode 102", progress=40
    )
    assert healthy == NextEpisodeCheck(
        state=NextCheckState.HEALTHY, code="S01E02", name="Episode 102"
    )
    assert history.call_count == 2  # done: no more reads


@respx.mock
async def test_a_repair_is_shown_with_infinidysks_message() -> None:
    _, history = series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    reads(history, history_item(ASKED_AT + timedelta(minutes=3), 1, 4, "Repaired from PAR2."))
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
    finally:
        await h.aclose()
    check = h.check()
    assert check is not None and check.state is NextCheckState.REPAIRED_PAR2
    assert check.message == "Repaired from PAR2." and check.progress is None


@respx.mock
async def test_no_result_within_the_follow_window() -> None:
    _, history = series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        assert (check := h.check()) is not None and check.state is NextCheckState.QUEUED
        h.now += FOLLOW_S
        await h.run()
        reads_before = history.call_count
        await h.run()
    finally:
        await h.aclose()
    check = h.check()
    assert check is not None and check.state is NextCheckState.NO_RESULT
    assert history.call_count == reads_before


@respx.mock
async def test_unreadable_infinidysk_keeps_waiting() -> None:
    _, history = series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    history.mock(return_value=httpx.Response(500))
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing())
        await h.run()
        waiting = h.check()
        reads(history, history_item(ASKED_AT + timedelta(seconds=30)))
        await h.run()
    finally:
        await h.aclose()
    assert waiting is not None and waiting.state is NextCheckState.QUEUED
    check = h.check()
    assert check is not None and check.state is NextCheckState.HEALTHY


@respx.mock
async def test_a_replay_shows_the_earlier_result() -> None:
    _, history = series()
    recheck = respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    reads(history, history_item(ASKED_AT + timedelta(seconds=35)))
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing(session_id="s1"))
        await h.run()
        h.now += 3600
        h.wall += timedelta(hours=1)
        h.play(playing(session_id="s2"))
        await h.run()
    finally:
        await h.aclose()
    assert recheck.call_count == 1
    check = h.check(session_id="s2")
    assert check is not None and check.state is NextCheckState.HEALTHY


@respx.mock
async def test_a_replay_without_a_recent_result_stops_following() -> None:
    _, history = series()
    respx.post(f"{IDK}/api/recheck-file").mock(return_value=accepted())
    h = Harness([emby(ON), infinidysk()])
    try:
        h.play(playing(session_id="s1"))
        await h.run()
        h.play(playing(session_id="s2"))  # s1 ended without a result
        await h.run()
        reads_before = history.call_count
        await h.run()
    finally:
        await h.aclose()
    check = h.check(session_id="s2")
    assert check is not None and check.state is NextCheckState.RECENT
    assert history.call_count == reads_before
