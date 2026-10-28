"""Normalized playback sessions (Emby today; shaped to fit other media servers)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import computed_field

from mediahub.models.common import Model, SourceRef


class PlayMethod(StrEnum):
    DIRECT_PLAY = "direct_play"
    DIRECT_STREAM = "direct_stream"
    TRANSCODE = "transcode"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "direct_play": "Direct Play",
            "direct_stream": "Direct Stream",
            "transcode": "Transcode",
        }.get(self.value, "Unknown")


class PlaybackState(StrEnum):
    PLAYING = "playing"
    PAUSED = "paused"


class HdrType(StrEnum):
    SDR = "sdr"
    HDR10 = "hdr10"
    HDR10_PLUS = "hdr10_plus"
    HLG = "hlg"
    DOLBY_VISION = "dolby_vision"

    @property
    def label(self) -> str:
        return {
            "sdr": "SDR",
            "hdr10": "HDR10",
            "hdr10_plus": "HDR10+",
            "hlg": "HLG",
            "dolby_vision": "Dolby Vision",
        }[self.value]


class MediaKind(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"
    MUSIC = "music"
    LIVE_TV = "live_tv"
    OTHER = "other"


class MediaInfo(Model):
    title: str
    subtitle: str | None = None  # e.g. "S02E06 · The Trial"
    year: int | None = None
    kind: MediaKind = MediaKind.OTHER
    image_url: str | None = None


class StreamInfo(Model):
    """Properties of the source file being played."""

    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    width: int | None = None
    height: int | None = None
    hdr: HdrType | None = None
    bitrate: int | None = None  # bits per second

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolution(self) -> str | None:
        if not self.height and not self.width:
            return None
        w, h = self.width or 0, self.height or 0
        if w >= 3200 or h >= 2000:
            return "4K"
        if w >= 1900 or h >= 1000:
            return "1080p"
        if w >= 1200 or h >= 700:
            return "720p"
        if h >= 400:
            return "480p"
        return f"{h}p" if h else None


class TranscodeInfo(Model):
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    framerate: float | None = None
    video_direct: bool | None = None
    audio_direct: bool | None = None
    hw_decode: bool | None = None
    hw_encode: bool | None = None
    tonemapping: bool = False
    reasons: list[str] = []
    progress_pct: float | None = None


class PlaybackSession(Model):
    source: SourceRef
    session_id: str
    user: str | None = None
    device: str | None = None
    client: str | None = None
    client_version: str | None = None
    remote_address: str | None = None  # stripped before data leaves this server
    file_name: str | None = None  # basename of the played file (e.g. "X.mkv.strm"), no path
    # STRM items: last path segment of the URL inside the .strm (InfiniDysk's item id). Only
    # that segment: the URL's query carries InfiniDysk's download key.
    strm_target: str | None = None
    # Emby ids of the playing item, its user and (episodes) its series: for the next-episode check
    item_id: str | None = None
    user_id: str | None = None
    series_id: str | None = None
    state: PlaybackState = PlaybackState.PLAYING
    position_s: float | None = None
    duration_s: float | None = None
    media: MediaInfo
    play_method: PlayMethod = PlayMethod.UNKNOWN
    source_stream: StreamInfo = StreamInfo()
    transcode: TranscodeInfo | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> float | None:
        if self.position_s is None or not self.duration_s:
            return None
        return max(0.0, min(100.0, self.position_s / self.duration_s * 100))


class NextCheckState(StrEnum):
    QUEUED = "queued"
    ALREADY_QUEUED = "already-queued"
    ALREADY_RUNNING = "already-running"
    RECENT = "recent"  # asked within the last 24 h: not asked again
    NO_NEXT = "no-next"  # the playing episode is the last one in the library
    NO_FILE = "no-file"  # the next episode has no InfiniDysk item id (not STRM, or lookup failed)
    NOT_FOUND = "not-found"  # no InfiniDysk has the file
    REFUSED = "refused"  # InfiniDysk declined, e.g. background repairs are off
    ERROR = "error"  # Emby or InfiniDysk couldn't be asked
    # Followed afterwards from InfiniDysk's active checks and history:
    CHECKING = "checking"
    REPAIRING = "repairing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # damaged, but within InfiniDysk's playable tolerance
    UNHEALTHY = "unhealthy"
    REPAIRED = "repaired"
    REPAIRED_PAR2 = "repaired-par2"
    DELETED = "deleted"
    ACTION_NEEDED = "action-needed"
    NO_RESULT = "no-result"  # no result showed up while it was followed

    @property
    def label(self) -> str:
        return {
            "queued": "health check queued",
            "already-queued": "health check already queued",
            "already-running": "being health-checked now",
            "recent": "check already requested in the last 24 h",
            "no-next": "No next episode in the library",
            "no-file": "no InfiniDysk file found",
            "not-found": "not in InfiniDysk",
            "refused": "InfiniDysk declined the check",
            "error": "couldn't request a health check",
            "checking": "being checked",
            "repairing": "being repaired",
            "healthy": "healthy",
            "degraded": "degraded",
            "unhealthy": "unhealthy",
            "repaired": "repaired",
            "repaired-par2": "repaired via PAR2",
            "deleted": "deleted",
            "action-needed": "action needed",
            "no-result": "no result yet",
        }[self.value]

    @property
    def tone(self) -> str | None:
        """None = muted: nothing to check, which is not a problem."""
        if self in _MUTED:
            return None
        if self in _WAITING:
            return "info"
        if self in (NextCheckState.HEALTHY, NextCheckState.REPAIRED, NextCheckState.REPAIRED_PAR2):
            return "success"
        return "warning"

    @property
    def icon(self) -> str:
        if self in (NextCheckState.CHECKING, NextCheckState.REPAIRING):
            return "loader-circle"
        if self in _WAITING:
            return "clock"
        return {"success": "shield-check", "warning": "triangle-alert"}.get(self.tone or "", "info")


_MUTED = frozenset({NextCheckState.NO_NEXT, NextCheckState.NO_FILE, NextCheckState.NO_RESULT})
_WAITING = frozenset(
    {
        NextCheckState.QUEUED,
        NextCheckState.ALREADY_QUEUED,
        NextCheckState.ALREADY_RUNNING,
        NextCheckState.RECENT,
        NextCheckState.CHECKING,
        NextCheckState.REPAIRING,
    }
)


class NextEpisodeCheck(Model):
    """Outcome of the next-episode health check for one playing episode. Local only."""

    state: NextCheckState
    code: str | None = None  # the next episode, e.g. "S01E03"
    name: str | None = None
    message: str | None = None  # InfiniDysk's or the integration's text, verbatim
    progress: int | None = None  # percent, while InfiniDysk is checking or repairing
