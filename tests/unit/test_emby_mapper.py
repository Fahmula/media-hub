"""Emby session mapping, checked against redacted captures from a real Emby 4.11 server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mediahub.integrations import image_refs
from mediahub.integrations.base import InstanceConfig
from mediahub.integrations.emby.integration import EmbyIntegration
from mediahub.integrations.emby.mapper import map_session
from mediahub.models.common import ServiceType, SourceRef
from mediahub.models.playback import HdrType, MediaKind, PlaybackSession, PlaybackState, PlayMethod

FIXTURES = Path(__file__).parent.parent / "fixtures" / "emby"
SOURCE = SourceRef(
    server_id="s",
    server_name="S",
    instance_id="emby-1",
    instance_name="Emby",
    service_type=ServiceType.EMBY,
)


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def fake_url(item_id: str, tag: str) -> str:
    return f"img:{item_id}:{tag}"


def sessions(name: str) -> dict[str, PlaybackSession]:
    """Mapped sessions keyed by the raw NowPlayingItem name."""
    out = {}
    for raw in fixture(name)["sessions"]:
        mapped = map_session(raw, SOURCE, fake_url)
        assert mapped is not None
        out[raw["NowPlayingItem"]["Name"]] = mapped
    return out


def test_episode_direct_stream() -> None:
    s = sessions("show-and-livetv")["Episode 1"]
    assert s.play_method is PlayMethod.DIRECT_STREAM and s.transcode is None
    assert s.media.kind is MediaKind.EPISODE
    assert (s.media.title, s.media.subtitle, s.media.year) == (
        "Series 1",
        "S01E01 · Episode 1",
        2022,
    )
    assert s.media.image_url == "img:1018754:c6b9154bb4088d72545705c15647f85e_638366872567701526"
    assert s.duration_s == pytest.approx(3906.965)
    assert s.progress_pct is not None and 0 < s.progress_pct < 100
    assert (s.user, s.device, s.client) == ("user1", "Device 1", "Emby Web")
    assert s.source_stream.hdr is HdrType.SDR
    assert (s.source_stream.video_codec, s.source_stream.audio_codec) == ("hevc", "aac")
    assert s.source_stream.resolution == "1080p"


def test_live_tv_uses_pipeline_for_hardware_flags() -> None:
    s = sessions("show-and-livetv")["Channel 1"]
    assert s.media.kind is MediaKind.LIVE_TV
    assert (s.media.title, s.media.subtitle) == ("Channel 1", "Program 2")
    assert s.duration_s is None and s.progress_pct is None
    t = s.transcode
    assert t is not None and s.play_method is PlayMethod.TRANSCODE
    # Live TV: Emby reports VideoDecoderIsHardware=false, but the pipeline runs on QuickSync.
    assert t.hw_decode is True and t.hw_encode is True
    assert t.reasons == ["ContainerBitrateExceedsLimit"]
    assert (t.container, t.video_codec, t.bitrate) == ("m3u8", "h264", 7_000_000)


def test_paused_and_movie_transcode() -> None:
    mapped = sessions("movie-and-paused")
    assert mapped["Episode 1"].state is PlaybackState.PAUSED
    movie = mapped["Movie 1"]
    assert movie.state is PlaybackState.PLAYING
    assert movie.media.kind is MediaKind.MOVIE and movie.media.year == 2026
    assert movie.media.subtitle is None
    t = movie.transcode
    assert t is not None and t.hw_decode and t.hw_encode and not t.tonemapping
    assert t.progress_pct == pytest.approx(2.667, abs=0.01)
    assert t.video_direct is False and t.audio_direct is True


def test_dolby_vision_audio_only_transcode() -> None:
    dv = sessions("hdr-and-direct-play")["Movie 1"]
    assert dv.source_stream.hdr is HdrType.DOLBY_VISION
    assert dv.source_stream.resolution == "4K"
    assert dv.source_stream.audio_codec == "truehd" and dv.source_stream.audio_channels == 8
    t = dv.transcode
    assert t is not None and dv.play_method is PlayMethod.TRANSCODE
    assert t.video_direct is True and t.audio_direct is False
    # Video is copied: the pipeline's "SW Decode" step isn't a real decode.
    assert t.hw_decode is None and t.hw_encode is None
    assert t.reasons == ["AudioCodecNotSupported"]


def test_dolby_vision_full_transcode_without_tone_mapping() -> None:
    t = sessions("hdr-tonemap")["Movie 1"].transcode
    assert t is not None
    assert t.video_direct is False and t.hw_decode and t.hw_encode
    assert t.tonemapping is False  # pipeline has Scale + Color Convert only
    assert (t.width, t.height) == (1920, 1080)


def test_tone_mapping_step_is_detected() -> None:
    raw = fixture("hdr-tonemap")["sessions"][0]
    steps = raw["TranscodingInfo"]["VideoPipelineInfo"]
    steps.insert(2, {"StepType": "ToneMapping", "Name": "Tone Mapping"})
    mapped = map_session(raw, SOURCE, fake_url)
    assert mapped is not None and mapped.transcode is not None and mapped.transcode.tonemapping


def test_remux_reported_as_direct_stream() -> None:
    raw = fixture("movie-and-paused")["sessions"][0]
    raw["TranscodingInfo"].update(IsVideoDirect=True, IsAudioDirect=True)
    mapped = map_session(raw, SOURCE, fake_url)
    assert mapped is not None
    assert mapped.play_method is PlayMethod.DIRECT_STREAM and mapped.transcode is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"Id": "x"},
        {"Id": "x", "NowPlayingItem": {}},
        {"NowPlayingItem": {"Name": "No session id"}},
    ],
)
def test_idle_or_malformed_sessions_are_skipped(raw: Any) -> None:
    assert map_session(raw, SOURCE, fake_url) is None


def test_minimal_and_odd_fields_do_not_crash() -> None:
    raw = {
        "Id": "x",
        "NowPlayingItem": {"Name": 5, "Type": "Episode", "MediaStreams": "nope", "ImageTags": []},
        "PlayState": {"PlayMethod": "Teleport", "PositionTicks": -1, "IsPaused": "yes"},
        "TranscodingInfo": {"VideoPipelineInfo": [None, 1], "TranscodeReasons": [1, "A"]},
    }
    s = map_session(raw, SOURCE, fake_url)
    assert s is not None
    assert s.media.title == "Unknown" and s.media.subtitle is None and s.media.image_url is None
    assert s.play_method is PlayMethod.UNKNOWN and s.transcode is None
    assert s.state is PlaybackState.PLAYING and s.position_s is None


@respx.mock
async def test_poll_maps_sessions_with_signed_images() -> None:
    capture = fixture("hdr-and-direct-play")
    respx.get("http://emby:8096/emby/System/Info").mock(
        return_value=httpx.Response(200, json={"Version": "4.11.0.9"})
    )
    respx.get("http://emby:8096/emby/Sessions").mock(
        return_value=httpx.Response(200, json=[*capture["sessions"], {"Id": "idle"}])
    )
    cfg = InstanceConfig(
        id="emby-1",
        service_type=ServiceType.EMBY,
        name="Emby",
        base_url="http://emby:8096",
        secrets={"api_key": "K"},
    )
    integration = EmbyIntegration(cfg, SOURCE)
    try:
        result = await integration.poll(full=False)
    finally:
        await integration.aclose()
    assert result.version == "4.11.0.9"
    assert result.sessions is not None and len(result.sessions) == 3
    url = result.sessions[0].media.image_url
    assert url is not None
    _, _, instance_id, item_id, tag, signature = url.split("/")
    assert image_refs.verify(instance_id, item_id, tag, signature)
    assert not image_refs.verify(instance_id, item_id, tag + "x", signature)
