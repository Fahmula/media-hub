"""Per-stream problems taken from InfiniDysk's own log.

InfiniDysk tracks per-read failovers, gap fills and repairs internally but exposes none of it
per read (``ar`` has no health fields, ``/api/get-overview-stats`` only aggregates). Its log
lines do name the file, so they are the only per-stream signal. They arrive on the ``log``
WebSocket event topic (``{"entries": [{seq, ts, level, msg, source, exception}]}``, not
replayed on subscribe) and from ``/api/get-logs?levels=&limit=`` (a read-only ring buffer
snapshot, used to backfill what happened before the feed connected).

``msg`` is Serilog's ``RenderMessage()``, so string values are wrapped in double quotes:
``File "/content/movies/X/X.mkv" has missing articles: …``. Templates (``main`` @ a72e11d7):

* ``Streams/MultiSegmentStream.cs`` and ``Streams/UnbufferedMultiSegmentStream.cs``:
  "Article {SegmentId} missing on all providers while reading {FileName}. Filling the
  {Bytes}-byte gap to preserve later file offsets." (also "persistently corrupt", "is a
  health-confirmed missing segment of", "could not be confirmed missing … for this read
  only") and ``SegmentHoleReporter.cs`` "Segment … decoded {Bytes} bytes short … Filling the
  gap": playback continues with zeroed data → *recovered*.
* "First article {SegmentId} missing on all providers at playback start while reading
  {FileName}. Failing the stream so the player surfaces an error." → *failed*.
* ``Middlewares/ExceptionMiddleware.cs`` (``{FilePath}`` is the DAV item path, same as the
  read's ``path``): "File {FilePath} has missing articles", "has corrupt articles", "could not
  seek", "contains a corrupt RAR", "cannot be served", "delivered {N} of {M} expected bytes",
  "could not be read from byte position", "provider authentication failed", "could not
  connect to usenet provider", "WebDAV read failed fast", "aborted after headers" → *failed*;
  "… could not be confirmed missing: {Reason}. Returning 503 so the client retries." →
  *retried*.
* ``Services/HealthCheckService.cs``: "Performing urgent dynamic repair for {FilePath}",
  "… Starting repair.", "classified {Path} as degraded" → *repair*.

Any other Warning/Error line naming the file is kept as a plain warning/failure. Messages are
shown verbatim. InfiniDysk suppresses duplicates per file for 60 s ("suppressed N duplicates
in last 60s"), so counts are lower bounds.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mediahub.models.infinidysk import ProblemOutcome, ReadProblem

MAX_LINES = 1000
MAX_AGE_S = 2 * 3600
MAX_PROBLEMS_PER_READ = 20
# A file name shorter than this is too generic to attribute a log line by name alone.
MIN_NAME_LEN = 6

# Checked in order: "First article … Failing the stream" also contains "missing on all".
_PATTERNS: tuple[tuple[str, ProblemOutcome], ...] = (
    ("Failing the stream", ProblemOutcome.FAILED),
    ("has missing articles", ProblemOutcome.FAILED),
    ("has corrupt articles", ProblemOutcome.FAILED),
    ("could not seek", ProblemOutcome.FAILED),
    ("contains a corrupt RAR", ProblemOutcome.FAILED),
    ("cannot be served", ProblemOutcome.FAILED),
    ("expected bytes from byte position", ProblemOutcome.FAILED),
    ("could not be read from byte position", ProblemOutcome.FAILED),
    ("provider authentication failed", ProblemOutcome.FAILED),
    ("could not connect to usenet provider", ProblemOutcome.FAILED),
    ("WebDAV read failed fast", ProblemOutcome.FAILED),
    ("aborted after headers", ProblemOutcome.FAILED),
    ("Returning 503 so the client retries", ProblemOutcome.RETRIED),
    ("Filling the", ProblemOutcome.RECOVERED),
    ("Performing urgent dynamic repair", ProblemOutcome.REPAIR),
    ("Starting repair", ProblemOutcome.REPAIR),
    ("as degraded", ProblemOutcome.REPAIR),
)
_LEVEL_FALLBACK = {
    "warning": ProblemOutcome.WARNING,
    "error": ProblemOutcome.FAILED,
    "fatal": ProblemOutcome.FAILED,
}


def classify(level: str, message: str) -> ProblemOutcome | None:
    for needle, outcome in _PATTERNS:
        if needle in message:
            return outcome
    return _LEVEL_FALLBACK.get(level.lower())


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts_ms: float
    outcome: ProblemOutcome
    message: str

    def mentions(self, path: str | None, file_name: str | None) -> bool:
        if path and path in self.message:
            return True
        return bool(file_name and len(file_name) >= MIN_NAME_LEN and file_name in self.message)


def parse_entry(raw: Any) -> LogLine | None:
    if not isinstance(raw, Mapping):
        return None
    seq, ts, level, msg = raw.get("seq"), raw.get("ts"), raw.get("level"), raw.get("msg")
    if not (
        isinstance(seq, int)
        and isinstance(ts, int | float)
        and isinstance(level, str)
        and isinstance(msg, str)
    ):
        return None
    if level.lower() in ("verbose", "debug"):
        return None
    outcome = classify(level, msg)
    if outcome is None:
        return None
    return LogLine(seq=seq, ts_ms=float(ts), outcome=outcome, message=msg.strip())


class LogBook:
    """Recent log lines that could concern a stream, deduplicated across feed and backfill."""

    def __init__(self) -> None:
        self._lines: deque[LogLine] = deque(maxlen=MAX_LINES)
        # (seq, ts) rather than seq alone: seq restarts at 1 when InfiniDysk restarts.
        self._keys: set[tuple[int, float]] = set()

    def __len__(self) -> int:
        return len(self._lines)

    def add(self, entries: Iterable[Any]) -> None:
        fresh = []
        for raw in entries:
            line = parse_entry(raw)
            if line is not None and (line.seq, line.ts_ms) not in self._keys:
                self._keys.add((line.seq, line.ts_ms))
                fresh.append(line)
        if not fresh:
            return
        lines = sorted([*self._lines, *fresh], key=lambda ln: (ln.ts_ms, ln.seq))
        # Relative to InfiniDysk's newest line, not our clock: the two hosts may disagree.
        cutoff = lines[-1].ts_ms - MAX_AGE_S * 1000
        self._lines = deque((ln for ln in lines if ln.ts_ms >= cutoff), maxlen=MAX_LINES)
        self._keys = {(ln.seq, ln.ts_ms) for ln in self._lines}

    def clear(self) -> None:
        self._lines.clear()
        self._keys.clear()

    def problems(
        self, path: str | None, file_name: str | None, since_ms: float | None
    ) -> list[ReadProblem]:
        out = [
            ReadProblem(
                at=datetime.fromtimestamp(ln.ts_ms / 1000, UTC),
                outcome=ln.outcome,
                message=ln.message,
            )
            for ln in self._lines
            if (since_ms is None or ln.ts_ms >= since_ms) and ln.mentions(path, file_name)
        ]
        return out[-MAX_PROBLEMS_PER_READ:]
