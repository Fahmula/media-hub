"""Map raw Sonarr/Radarr API v3 queue and history records to normalized models.

Wire format taken from the Sonarr v4 (v4.0.20) and Radarr v5.8+/v6 sources, since the
interesting states (failed, blocked) are hard to capture on demand:

* Null properties are omitted, enums are camelCase strings (``eventType`` too; its numeric
  values differ between the apps), dates are ``yyyy-MM-ddTHH:mm:ssZ`` and ``timeleft`` is a
  .NET TimeSpan (``[-][d.]hh:mm:ss[.fffffff]``).
* Sonarr v4 returns one queue record per episode, so season packs are grouped by
  ``downloadId``. Pending (delay profile) records have no ``downloadId`` or tracked fields.
* A single-file import rejection stays ``importPending`` + ``warning``; only multi-file,
  ID-match and title-mismatch rejections become ``importBlocked`` (Sonarr 4.0.6+, Radarr 5.8+).
  The warning states are retried every cycle and often clear by themselves (seen live: "No
  files found are eligible for import" while a post-processor, FileFlows, was still
  converting the download), so they map to ``IMPORT_WARNING`` rather than ``IMPORT_BLOCKED``.
* ``failedPending``/``failed`` leave the queue within one processing pass, so failures mostly
  show up as ``downloadFailed`` history events.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mediahub.models.common import SourceRef
from mediahub.models.downloads import (
    ActivityEvent,
    ActivityKind,
    DownloadItem,
    DownloadState,
    StatusMessage,
)

# (series or movie id, image tag) -> browser URL for that image, or None.
ImageUrl = Callable[[str, str], str | None]

POSTER_TAG = "poster-250"

_TIMESPAN = re.compile(r"^(-)?(?:(\d+)\.)?(\d+):(\d{2}):(\d{2})(?:\.(\d+))?$")
_WAITING = {"queued", "paused", "delay", "downloadClientUnavailable", "fallback"}
_EVENT_KINDS = {
    "grabbed": ActivityKind.GRABBED,
    "downloadFolderImported": ActivityKind.IMPORTED,
    "seriesFolderImported": ActivityKind.IMPORTED,
    "movieFolderImported": ActivityKind.IMPORTED,
    "downloadFailed": ActivityKind.FAILED,
    "episodeFileDeleted": ActivityKind.DELETED,
    "movieFileDeleted": ActivityKind.DELETED,
    "episodeFileRenamed": ActivityKind.RENAMED,
    "movieFileRenamed": ActivityKind.RENAMED,
    "downloadIgnored": ActivityKind.IGNORED,
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def parse_timespan(value: Any) -> float | None:
    """Seconds from a .NET TimeSpan string such as ``"1.02:03:04.5000000"``."""
    match = _TIMESPAN.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    sign, days, hours, minutes, seconds, fraction = match.groups()
    total: float = int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    total += float(f"0.{fraction}") if fraction else 0.0
    return -total if sign else total


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def map_state(record: dict[str, Any]) -> DownloadState:
    """First match wins; the raw values are always shown alongside."""
    status = record.get("status")
    state = record.get("trackedDownloadState")
    tracked = record.get("trackedDownloadStatus")
    if state in ("failedPending", "failed") or status == "failed" or tracked == "error":
        return DownloadState.FAILED
    if state == "importBlocked":
        return DownloadState.IMPORT_BLOCKED
    if status == "completed" and tracked == "warning":
        return DownloadState.IMPORT_WARNING
    if state == "importing":
        return DownloadState.IMPORTING
    if state == "importPending" or status == "completed":
        return DownloadState.AWAITING_IMPORT
    if status == "warning" or tracked == "warning":
        return DownloadState.STALLED
    if status in _WAITING:
        return DownloadState.WAITING
    if status == "downloading":
        return DownloadState.DOWNLOADING
    return DownloadState.UNKNOWN


def episode_label(episodes: list[dict[str, Any]]) -> str | None:
    """``S02E03`` for one episode, ``S02E01–E08`` within a season, ``S01–S03`` across seasons."""
    numbers = sorted(
        {
            (s, e)
            for ep in episodes
            if (s := _int(ep.get("seasonNumber"))) is not None
            and (e := _int(ep.get("episodeNumber"))) is not None
        }
    )
    if not numbers:
        return None
    (first_s, first_e), (last_s, last_e) = numbers[0], numbers[-1]
    if len(numbers) == 1:
        return f"S{first_s:02d}E{first_e:02d}"
    if first_s == last_s:
        return f"S{first_s:02d}E{first_e:02d}–E{last_e:02d}"
    return f"S{first_s:02d}–S{last_s:02d}"


def _media(record: dict[str, Any], image_url: ImageUrl) -> tuple[str | None, str | None]:
    """(title, poster URL) of the series or movie a record belongs to."""
    series, movie = _dict(record.get("series")), _dict(record.get("movie"))
    media_id = _int(record.get("seriesId")) or _int(record.get("movieId"))
    title = _str(series.get("title")) or _str(movie.get("title"))
    image = image_url(str(media_id), POSTER_TAG) if media_id else None
    return title, image


def _subtitle(record: dict[str, Any], episodes: list[dict[str, Any]]) -> str | None:
    movie = _dict(record.get("movie"))
    if movie:
        year = _int(movie.get("year"))
        return str(year) if year else None
    label = episode_label(episodes)
    if label and len(episodes) == 1 and (name := _str(episodes[0].get("title"))):
        return f"{label} · {name}"
    return label


def _media_ids(rows: list[dict[str, Any]]) -> list[str]:
    """Episode ids (Sonarr) or the movie id (Radarr), to match failures with later grabs."""
    ids: set[str] = set()
    for row in rows:
        if episode := _int(row.get("episodeId")):
            ids.add(f"e{episode}")
        elif movie := _int(row.get("movieId")):
            ids.add(f"m{movie}")
    return sorted(ids)


def _messages(raw: Any) -> list[StatusMessage]:
    out = []
    for entry in _list(raw):
        entry = _dict(entry)
        lines = [m for m in _list(entry.get("messages")) if isinstance(m, str) and m]
        title = _str(entry.get("title"))
        if title or lines:
            out.append(StatusMessage(title=title, messages=lines))
    return out


def _download(rows: list[dict[str, Any]], source: SourceRef, image_url: ImageUrl) -> DownloadItem:
    first = rows[0]
    release = _str(first.get("title"))
    episodes = [ep for r in rows if (ep := _dict(r.get("episode")))]
    title, image = _media(first, image_url)
    size = _int(first.get("size"))
    quality = _str(_dict(_dict(first.get("quality")).get("quality")).get("name"))
    return DownloadItem(
        source=source,
        item_id=str(first.get("id", "")),
        download_id=_str(first.get("downloadId")),
        title=title or release or "Unknown download",
        subtitle=_subtitle(first, episodes) if title else None,
        release_title=release if title else None,
        image_url=image,
        protocol=_str(first.get("protocol")),
        client=_str(first.get("downloadClient")),
        indexer=_str(first.get("indexer")),
        quality=quality,
        size_bytes=size if size and size > 0 else None,
        remaining_bytes=_int(first.get("sizeleft")),
        eta_s=parse_timespan(first.get("timeleft")),
        added_at=parse_datetime(first.get("added")),
        state=map_state(first),
        raw_status=_str(first.get("status")),
        raw_tracked_state=_str(first.get("trackedDownloadState")),
        raw_tracked_status=_str(first.get("trackedDownloadStatus")),
        messages=_messages(first.get("statusMessages")),
        error_message=_str(first.get("errorMessage")),
        episode_count=max(len(episodes), 1),
        media_ids=_media_ids(rows),
    )


def map_queue(records: Any, source: SourceRef, image_url: ImageUrl) -> list[DownloadItem]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in _list(records):
        if not isinstance(record, dict) or record.get("id") is None:
            continue
        key = _str(record.get("downloadId")) or f"id:{record['id']}"
        groups.setdefault(key, []).append(record)
    return [_download(rows, source, image_url) for rows in groups.values()]


def _event_detail(kind: ActivityKind, record: dict[str, Any]) -> str | None:
    data = _dict(record.get("data"))
    quality = _str(_dict(_dict(record.get("quality")).get("quality")).get("name"))
    if kind in (ActivityKind.FAILED, ActivityKind.IGNORED):
        return _str(data.get("message"))
    if kind is ActivityKind.GRABBED:
        return " · ".join(p for p in (quality, _str(data.get("indexer"))) if p) or None
    if kind is ActivityKind.IMPORTED:
        return quality
    if kind is ActivityKind.DELETED and (reason := _str(data.get("reason"))):
        return f"Reason: {reason}"
    return None


def map_history(records: Any, source: SourceRef, image_url: ImageUrl) -> list[ActivityEvent]:
    """One event per history row, except Sonarr's per-episode rows of one download."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in _list(records):
        if not isinstance(record, dict) or record.get("id") is None:
            continue
        event = str(record.get("eventType", ""))
        key = (event, _str(record.get("downloadId")) or f"id:{record['id']}")
        groups.setdefault(key, []).append(record)

    events = []
    for (event, _), rows in groups.items():
        first = rows[0]
        at = parse_datetime(first.get("date"))
        if at is None:
            continue
        kind = _EVENT_KINDS.get(event, ActivityKind.OTHER)
        title, image = _media(first, image_url)
        episodes = [ep for r in rows if (ep := _dict(r.get("episode")))]
        subtitle = _subtitle(first, episodes) if title else None
        if len(episodes) > 1 and subtitle:
            subtitle = f"{subtitle} · {len(episodes)} episodes"
        events.append(
            ActivityEvent(
                source=source,
                event_id=str(first["id"]),
                download_id=_str(first.get("downloadId")),
                kind=kind,
                title=title or _str(first.get("sourceTitle")) or "Unknown",
                subtitle=subtitle,
                image_url=image,
                at=at,
                detail=_event_detail(kind, first),
                media_ids=_media_ids(rows),
            )
        )
    return events
