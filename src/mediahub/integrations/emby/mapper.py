"""Map raw Emby ``/Sessions`` entries to normalized ``PlaybackSession`` models.

Field behaviour verified against Emby 4.11 captures in ``tests/fixtures/emby/``:

* ``VideoDecoderIsHardware``/``VideoEncoderIsHardware`` can be ``false`` (Live TV) while the
  ``VideoPipelineInfo`` steps run on QuickSync, so a hardware step counts as hardware too.
* ``TranscodingInfo.Framerate`` is the transcoder's speed in fps, not the output frame rate.
* 10-bit SDR exists; HDR comes from ``ExtendedVideoType``/``ColorTransfer``, not bit depth.
* When the video is copied, the pipeline still lists a "SW Decode" step; it isn't decoding.

Every field is treated as optional: Emby omits keys freely between clients and item types.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit

from mediahub.models.common import SourceRef
from mediahub.models.playback import (
    HdrType,
    MediaInfo,
    MediaKind,
    PlaybackSession,
    PlaybackState,
    PlayMethod,
    StreamInfo,
    TranscodeInfo,
)

TICKS_PER_SECOND = 10_000_000

# (item_id, image_tag) -> browser URL for that image, or None when it can't be served.
ImageUrl = Callable[[str, str], str | None]

_KINDS = {
    "Movie": MediaKind.MOVIE,
    "Episode": MediaKind.EPISODE,
    "Audio": MediaKind.MUSIC,
    "TvChannel": MediaKind.LIVE_TV,
    "Program": MediaKind.LIVE_TV,
}
_PLAY_METHODS = {
    "DirectPlay": PlayMethod.DIRECT_PLAY,
    "DirectStream": PlayMethod.DIRECT_STREAM,
    "Transcode": PlayMethod.TRANSCODE,
}
_EXTENDED_HDR = {
    "DolbyVision": HdrType.DOLBY_VISION,
    "Hdr10": HdrType.HDR10,
    "Hdr10Plus": HdrType.HDR10_PLUS,
    "HyperLogGamma": HdrType.HLG,
}
_TRANSFER_HDR = {"smpte2084": HdrType.HDR10, "arib-std-b67": HdrType.HLG}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _seconds(ticks: Any) -> float | None:
    return ticks / TICKS_PER_SECOND if isinstance(ticks, int | float) and ticks >= 0 else None


def _lower(value: Any) -> str | None:
    text = _str(value)
    return text.lower() if text else None


def _basename(path: str | None) -> str | None:
    if not path:
        return None
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or None


def strm_target(url: str | None) -> str | None:
    """Last path segment of a STRM target URL, e.g. InfiniDysk's
    ``http://host:3000/view/.ids/4/0/c/6/b/<item id>?downloadKey=…`` → ``<item id>``.
    The query (a secret download key) is never looked at."""
    if not url or "://" not in url:
        return None
    return _basename(unquote(urlsplit(url).path))


def hdr_type(video: dict[str, Any]) -> HdrType | None:
    if not video:
        return None
    if extended := _EXTENDED_HDR.get(video.get("ExtendedVideoType") or ""):
        return extended
    if video.get("VideoRange") == "DolbyVision":
        return HdrType.DOLBY_VISION
    if transfer := _TRANSFER_HDR.get(_lower(video.get("ColorTransfer")) or ""):
        return transfer
    return HdrType.SDR if video.get("VideoRange") == "SDR" else None


def _streams(item: dict[str, Any], play: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    streams = [s for s in _list(item.get("MediaStreams")) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("Type") == "Video"), {})
    audios = [s for s in streams if s.get("Type") == "Audio"]
    index = play.get("AudioStreamIndex")
    audio = next(
        (s for s in audios if s.get("Index") == index),
        next((s for s in audios if s.get("IsDefault")), audios[0] if audios else {}),
    )
    return video, audio


def _first_image(image_url: ImageUrl, *candidates: tuple[Any, Any]) -> str | None:
    """URL for the first (item_id, tag) pair where both are present."""
    for item_id, tag in candidates:
        if _str(item_id) and _str(tag):
            return image_url(item_id, tag)
    return None


def episode_code(item: dict[str, Any]) -> str | None:
    """``S02E06`` from an episode item, or None when Emby has no numbers for it."""
    season, episode = _int(item.get("ParentIndexNumber")), _int(item.get("IndexNumber"))
    return f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else None


def _media(item: dict[str, Any], image_url: ImageUrl) -> MediaInfo:
    kind = _KINDS.get(item.get("Type") or "", MediaKind.OTHER)
    name = _str(item.get("Name")) or "Unknown"
    year = _int(item.get("ProductionYear"))
    own = (item.get("Id"), _dict(item.get("ImageTags")).get("Primary"))
    if kind is MediaKind.EPISODE:
        code = episode_code(item)
        series = (item.get("SeriesId"), item.get("SeriesPrimaryImageTag"))
        series_name = _str(item.get("SeriesName"))
        episode_name = _str(item.get("Name")) if series_name else None  # else it's the title
        return MediaInfo(
            title=series_name or name,
            subtitle=" · ".join(p for p in (code, episode_name) if p) or None,
            year=year,
            kind=kind,
            image_url=_first_image(image_url, series, own),  # portrait series poster first
        )
    if kind is MediaKind.LIVE_TV:
        program = _dict(item.get("CurrentProgram"))
        program_image = (program.get("Id"), _dict(program.get("ImageTags")).get("Primary"))
        return MediaInfo(
            title=name,
            subtitle=_str(program.get("Name")),
            kind=kind,
            image_url=_first_image(image_url, program_image, own),
        )
    if kind is MediaKind.MUSIC:
        album = (item.get("AlbumId"), item.get("AlbumPrimaryImageTag"))
        artists = ", ".join(a for a in _list(item.get("Artists")) if isinstance(a, str))
        subtitle = " · ".join(p for p in (artists, _str(item.get("Album"))) if p)
        return MediaInfo(
            title=name,
            subtitle=subtitle or None,
            year=year,
            kind=kind,
            image_url=_first_image(image_url, own, album),
        )
    return MediaInfo(title=name, year=year, kind=kind, image_url=_first_image(image_url, own))


def _pipeline_hw(steps: list[Any], step_type: str) -> bool:
    return any(
        isinstance(s, dict)
        and s.get("StepType") == step_type
        and s.get("IsHardwareContext") is True
        for s in steps
    )


def _transcode(info: dict[str, Any]) -> TranscodeInfo:
    steps = _list(info.get("VideoPipelineInfo"))
    video_direct = _bool(info.get("IsVideoDirect"))
    hw_decode = hw_encode = None
    if video_direct is not True:  # copied video has no real decode/encode
        hw_decode = info.get("VideoDecoderIsHardware") is True or _pipeline_hw(steps, "Decoder")
        hw_encode = info.get("VideoEncoderIsHardware") is True or _pipeline_hw(steps, "Encoder")
    # Not yet seen in a capture (tone mapping was off on the test server); matches Emby's
    # "ToneMapping" pipeline step type and names like "Tone Mapping".
    tonemapping = any(
        isinstance(s, dict)
        and "tonemap" in f"{s.get('StepType', '')}{s.get('Name', '')}".replace(" ", "").lower()
        for s in steps
    )
    progress = info.get("CompletionPercentage")
    return TranscodeInfo(
        container=_lower(info.get("Container")),
        video_codec=_lower(info.get("VideoCodec")),
        audio_codec=_lower(info.get("AudioCodec")),
        bitrate=_int(info.get("Bitrate")),
        width=_int(info.get("Width")),
        height=_int(info.get("Height")),
        video_direct=video_direct,
        audio_direct=_bool(info.get("IsAudioDirect")),
        hw_decode=hw_decode,
        hw_encode=hw_encode,
        tonemapping=tonemapping,
        reasons=[r for r in _list(info.get("TranscodeReasons")) if isinstance(r, str)],
        progress_pct=float(progress) if isinstance(progress, int | float) else None,
    )


def map_session(raw: Any, source: SourceRef, image_url: ImageUrl) -> PlaybackSession | None:
    """Return the normalized session, or None for idle sessions (nothing playing)."""
    session = _dict(raw)
    item = _dict(session.get("NowPlayingItem"))
    session_id = _str(session.get("Id"))
    if not item or not session_id:
        return None
    play = _dict(session.get("PlayState"))
    info = _dict(session.get("TranscodingInfo"))
    method = _PLAY_METHODS.get(play.get("PlayMethod") or "", PlayMethod.UNKNOWN)
    transcode: TranscodeInfo | None = None
    if method is PlayMethod.TRANSCODE and info:
        transcode = _transcode(info)
        if transcode.video_direct and transcode.audio_direct:
            method, transcode = PlayMethod.DIRECT_STREAM, None  # remux: nothing re-encoded
    video, audio = _streams(item, play)
    return PlaybackSession(
        source=source,
        session_id=session_id,
        user=_str(session.get("UserName")),
        device=_str(session.get("DeviceName")),
        client=_str(session.get("Client")),
        client_version=_str(session.get("ApplicationVersion")),
        remote_address=_str(session.get("RemoteEndPoint")),
        file_name=_basename(_str(item.get("Path"))),
        item_id=_str(item.get("Id")),
        user_id=_str(session.get("UserId")),
        series_id=_str(item.get("SeriesId")),
        state=PlaybackState.PAUSED if play.get("IsPaused") is True else PlaybackState.PLAYING,
        position_s=_seconds(play.get("PositionTicks")),
        duration_s=_seconds(item.get("RunTimeTicks")),
        media=_media(item, image_url),
        play_method=method,
        source_stream=StreamInfo(
            container=_lower(item.get("Container")),
            video_codec=_lower(video.get("Codec")),
            audio_codec=_lower(audio.get("Codec")),
            audio_channels=_int(audio.get("Channels")),
            width=_int(video.get("Width")) or _int(item.get("Width")),
            height=_int(video.get("Height")) or _int(item.get("Height")),
            hdr=hdr_type(video),
            bitrate=_int(item.get("Bitrate")) or _int(video.get("BitRate")),
        ),
        transcode=transcode,
    )
