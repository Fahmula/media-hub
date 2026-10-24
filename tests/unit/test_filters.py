from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mediahub.web.filters import (
    bitrate,
    clock,
    duration,
    filesize,
    reltime,
    short_reason,
    sparkline,
)


def test_formatting() -> None:
    assert filesize(None) == "—"
    assert filesize(512) == "512 B"
    assert filesize(1.5 * 1024**3) == "1.5 GB"
    assert filesize(10 * 1024**2, rate=True) == "10.0 MB/s"
    assert bitrate(23_400_000) == "23.4 Mbps"
    assert clock(4355) == "1:12:35"
    assert clock(734) == "12:14"
    assert duration(192) == "3m 12s"
    assert duration(7500) == "2h 5m"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert reltime(now - timedelta(hours=3), now) == "3h ago"
    assert reltime(now + timedelta(minutes=5), now) == "in 5m"
    assert reltime(None) == "never"
    assert short_reason("VideoCodecNotSupported") == "video codec not supported"
    assert sparkline([1.0]) == {"line": "", "area": ""}
    assert sparkline([1.0, 2.0])["line"].startswith("M0.0,")
