"""Jinja filters for human-friendly numbers and times."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def filesize(value: float | int | None, rate: bool = False) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            text = f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
            return f"{text}/s" if rate else text
        size /= 1024
    raise AssertionError("unreachable")


def bitrate(value: float | int | None) -> str:
    """Bits per second → '23.4 Mbps'."""
    if not value:
        return "—"
    bps = float(value)
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} kbps"
    return f"{bps:.0f} bps"


def clock(seconds: float | int | None) -> str:
    """Seconds → 'H:MM:SS' or 'M:SS'."""
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def duration(value: float | int | timedelta | None) -> str:
    """Compact human duration: '3m 12s', '2h 5m', '3d 4h'."""
    if value is None:
        return "—"
    seconds = int(value.total_seconds() if isinstance(value, timedelta) else value)
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s" if s and minutes < 10 else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"


def reltime(value: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5m ago', '3h ago', '2d ago'; future times read 'in 5m'."""
    if value is None:
        return "never"
    now = now or datetime.now(UTC)
    delta = (now - value).total_seconds()
    future = delta < 0
    delta = abs(delta)
    if delta < 10:
        return "just now"
    if delta < 60:
        text = f"{int(delta)}s"
    elif delta < 3600:
        text = f"{int(delta // 60)}m"
    elif delta < 86400:
        text = f"{int(delta // 3600)}h"
    else:
        text = f"{int(delta // 86400)}d"
    return f"in {text}" if future else f"{text} ago"


def pct(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def isotime(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value else ""


FILTERS: dict[str, Any] = {
    "filesize": filesize,
    "bitrate": bitrate,
    "clock": clock,
    "duration": duration,
    "reltime": reltime,
    "pct": pct,
    "isotime": isotime,
}
