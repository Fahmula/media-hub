"""InfiniDysk stream health: log classification, attribution, and matching to Emby sessions.

Messages are InfiniDysk's templates (``main`` @ a72e11d7) rendered the way Serilog does it:
string values in double quotes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from mediahub.aggregation.dashboard import match_reads
from mediahub.integrations.infinidysk.logs import LogBook, classify, parse_entry
from mediahub.integrations.infinidysk.ws import IDLE_LINGER_S, LiveFeed
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.infinidysk import (
    ActiveRead,
    InfiniDyskStats,
    ProblemOutcome,
    ReadProblem,
    StreamHealth,
)
from mediahub.models.playback import MediaInfo, PlaybackSession
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot
from mediahub.peers.redact import redact_service

NAME = "Movie.1.2026.2160p.WEB-DL.mkv"
PATH = f"/content/movies/Movie.1.2026.2160p.WEB-DL/{NAME}"
T0 = 1_800_000_000_000.0  # unix ms


def entry(msg: str, level: str = "Warning", seq: int = 1, ts: float = T0) -> dict[str, Any]:
    return {"seq": seq, "ts": ts, "level": level, "msg": msg, "source": "x"}


# ----- classification ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("level", "msg", "outcome"),
    [
        (
            "Warning",
            f'Article "a@b" missing on all providers while reading "{NAME}". Filling the '
            "768000-byte gap to preserve later file offsets.",
            ProblemOutcome.RECOVERED,
        ),
        (
            "Warning",
            f'Article "a@b" persistently corrupt while reading "{NAME}". Filling the 768000-byte '
            "gap to preserve later file offsets.",
            ProblemOutcome.RECOVERED,
        ),
        (
            "Warning",
            f'Article "a@b" is a health-confirmed missing segment of "{NAME}". Filling the '
            "768000-byte gap without a provider request.",
            ProblemOutcome.RECOVERED,
        ),
        (
            "Warning",
            f'Segment "a@b" of "{NAME}" decoded 12 bytes short of its recorded size. Filling the '
            "gap to keep the rest of the file aligned.",
            ProblemOutcome.RECOVERED,
        ),
        (
            "Warning",
            f'First article "a@b" missing on all providers at playback start while reading '
            f'"{NAME}". Failing the stream so the player surfaces an error.',
            ProblemOutcome.FAILED,
        ),
        (
            "Error",
            f'File "{PATH}" has missing articles: Article with message-id a@b not found.',
            ProblemOutcome.FAILED,
        ),
        ("Error", f'File "{PATH}" has corrupt articles: bad yEnc', ProblemOutcome.FAILED),
        (
            "Warning",
            f'File "{PATH}" could not seek to byte position 123. Reason: x',
            ProblemOutcome.FAILED,
        ),
        (
            "Warning",
            f'File "{PATH}" delivered 1 of 2 expected bytes from byte position 0 (client "Lavf")',
            ProblemOutcome.FAILED,
        ),
        (
            "Warning",
            f'File "{PATH}" article "a@b" could not be confirmed missing: timeout. Returning 503 '
            "so the client retries.",
            ProblemOutcome.RETRIED,
        ),
        ("Information", f'Performing urgent dynamic repair for "{PATH}"', ProblemOutcome.REPAIR),
        (
            "Warning",
            f'Health check classified "{PATH}" as degraded: 2 holes Playback fills the gaps; '
            "repair skipped.",
            ProblemOutcome.REPAIR,
        ),
        ("Warning", f'Stalled WebDAV stream. Method=GET Path="{PATH}"', ProblemOutcome.WARNING),
        ("Error", f'Something new about "{PATH}"', ProblemOutcome.FAILED),
        ("Information", f'Opened "{PATH}"', None),
    ],
)
def test_classify(level: str, msg: str, outcome: ProblemOutcome | None) -> None:
    assert classify(level, msg) is outcome


def test_parse_entry_skips_debug_and_malformed() -> None:
    assert parse_entry(entry(f'File "{PATH}" has missing articles: x', level="Debug")) is None
    assert parse_entry({"seq": "1", "ts": T0, "level": "Error", "msg": "x"}) is None
    assert parse_entry("nope") is None


# ----- attribution ---------------------------------------------------------------------------
def test_problems_match_by_path_or_name_and_window() -> None:
    book = LogBook()
    book.add(
        [
            entry(f'File "{PATH}" has missing articles: x', "Error", seq=1, ts=T0 + 2000),
            entry(f'Article "a" missing … reading "{NAME}". Filling the gap', seq=2, ts=T0 + 3000),
            entry('File "/content/other/Other.mkv" has missing articles: x', "Error", seq=3),
            entry(f'File "{PATH}" has corrupt articles: old', "Error", seq=4, ts=T0 - 60_000),
        ]
    )
    problems = book.problems(PATH, NAME, since_ms=T0)
    assert [p.outcome for p in problems] == [ProblemOutcome.FAILED, ProblemOutcome.RECOVERED]
    assert problems[0].at == datetime.fromtimestamp((T0 + 2000) / 1000, UTC)
    assert book.problems(None, NAME, since_ms=None)[-1].outcome is ProblemOutcome.RECOVERED
    # A file name too short to be specific never matches on its own.
    book.add([entry('Article "a" while reading "a.mkv". Filling the gap', seq=9)])
    assert book.problems(None, "a.mkv", since_ms=None) == []


def test_dedupe_across_backfill_and_restart() -> None:
    book = LogBook()
    line = entry(f'File "{PATH}" has missing articles: x', "Error", seq=5, ts=T0)
    book.add([line])
    book.add([line])  # the backfill repeats what the live topic already sent
    assert len(book) == 1
    # InfiniDysk restarted: seq starts over, but the timestamp differs.
    book.add([{**line, "ts": T0 + 10_000}])
    assert len(book) == 2


def test_old_lines_are_pruned_relative_to_newest() -> None:
    book = LogBook()
    book.add([entry(f'File "{PATH}" has missing articles: x', "Error", seq=1, ts=T0)])
    book.add([entry(f'File "{PATH}" has corrupt articles: x', "Error", seq=2, ts=T0 + 3 * 3.6e6)])
    assert len(book) == 1


def test_health_from_problems() -> None:
    def read(*outcomes: ProblemOutcome) -> ActiveRead:
        problems = [ReadProblem(at=datetime.now(UTC), outcome=o, message="m") for o in outcomes]
        return ActiveRead(read_id="r", file_name=NAME, problems=problems)

    assert read().health is StreamHealth.OK
    assert read(ProblemOutcome.WARNING).health is StreamHealth.WARNING
    assert read(ProblemOutcome.WARNING, ProblemOutcome.REPAIR).health is StreamHealth.RECOVERED
    assert read(ProblemOutcome.RECOVERED, ProblemOutcome.FAILED).health is StreamHealth.FAILED


# ----- live feed -----------------------------------------------------------------------------
def _ws(topic: str, payload: Any) -> str:
    return json.dumps({"Topic": topic, "Message": json.dumps(payload)})


def _raw_read(read_id: str, started: float, last: float) -> dict[str, Any]:
    return {
        "id": read_id,
        "fileName": NAME,
        "path": PATH,
        "startedAt": started,
        "lastActivityAt": last,
        "bytesRead": 1,
    }


def test_feed_attaches_log_lines_across_a_reopened_read() -> None:
    feed = LiveFeed("ws://unused", "k")
    feed.connected = True
    feed.handle(_ws("ar", {"reads": [_raw_read("a", T0, T0 + 1000)]}))
    feed.handle(
        _ws("log", {"entries": [entry(f'File "{PATH}" has missing articles: x', "Error", 1, T0)]})
    )
    # The player reopened the file: new read id, the old one lingers as idle.
    feed.handle(_ws("ar", {"reads": [_raw_read("b", T0 + 30_000, T0 + 31_000)]}))
    reads = feed.state().reads
    assert [r.read_id for r in reads] == ["b"]  # same file: the idle copy is replaced
    assert reads[0].health is StreamHealth.FAILED  # the earlier read's problem still counts


def test_feed_new_playback_starts_clean() -> None:
    feed = LiveFeed("ws://unused", "k")
    feed.connected = True
    old = T0 - (IDLE_LINGER_S + 60) * 1000
    feed.handle(
        _ws("log", {"entries": [entry(f'File "{PATH}" has missing articles: x', "Error", 1, old)]})
    )
    feed.handle(_ws("ar", {"reads": [_raw_read("c", T0, T0 + 1000)]}))
    assert feed.state().reads[0].health is StreamHealth.OK


def test_feed_remembers_files_after_the_read_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emby's transcoder buffers ahead: InfiniDysk can have no read for many minutes."""
    feed = LiveFeed("ws://unused", "k")
    feed.connected = True
    clock = [T0 / 1000]
    monkeypatch.setattr("mediahub.integrations.infinidysk.ws.time.time", lambda: clock[0])
    feed.handle(_ws("ar", {"reads": [_raw_read("a", T0, T0 + 1000)]}))
    feed.handle(
        _ws("log", {"entries": [entry(f'File "{PATH}" has missing articles: x', "Error", 1, T0)]})
    )
    feed.handle(_ws("ar", {"reads": []}))
    clock[0] += IDLE_LINGER_S + 600  # long past the idle linger
    state = feed.state()
    assert state.reads == []
    assert [r.read_id for r in state.recent] == ["a"]
    assert state.recent[0].health is StreamHealth.FAILED and state.recent[0].idle
    # The next burst, 20 minutes later, continues the same playback.
    later = T0 + 20 * 60_000
    feed.handle(_ws("ar", {"reads": [_raw_read("b", later, later + 1000)]}))
    state = feed.state()
    assert [r.read_id for r in state.reads] == ["b"] and state.recent == []
    assert state.reads[0].health is StreamHealth.FAILED


# ----- matching to Emby sessions -------------------------------------------------------------
def _src(instance: str, service: ServiceType, server: str = "srv") -> SourceRef:
    return SourceRef(
        server_id=server,
        server_name=server,
        instance_id=instance,
        instance_name=instance,
        service_type=service,
    )


def _session(file_name: str | None, sid: str = "s1", server: str = "srv") -> PlaybackSession:
    return PlaybackSession(
        source=_src("emby", ServiceType.EMBY, server),
        session_id=sid,
        file_name=file_name,
        media=MediaInfo(title="Movie 1"),
    )


def _server(
    sessions: list[PlaybackSession], reads: list[ActiveRead], *, live: bool = True
) -> ServerSnapshot:
    return ServerSnapshot(
        server_id="srv",
        server_name="srv",
        generated_at=datetime.now(UTC),
        services=[
            ServiceSnapshot(
                source=_src("emby", ServiceType.EMBY), status=ServiceStatus.OK, sessions=sessions
            ),
            ServiceSnapshot(
                source=_src("idk", ServiceType.INFINIDYSK),
                status=ServiceStatus.OK,
                infinidysk=InfiniDyskStats(live=live, reads=reads),
            ),
        ],
    )


def test_match_through_strm_and_prefer_live_read() -> None:
    idle = ActiveRead(read_id="old", file_name=NAME, idle=True)
    live = ActiveRead(read_id="new", file_name=NAME)
    other = ActiveRead(read_id="x", file_name="Other.mkv")
    sessions = [_session(f"{NAME}.strm"), _session(None, "s2"), _session("Nope.mkv", "s3")]
    matched = match_reads(_server(sessions, [idle, other, live]))
    assert {k[1]: r.read_id for k, r in matched.items()} == {"s1": "new"}
    # Symlink libraries play the file itself; names compare case-insensitively.
    assert match_reads(_server([_session(NAME.upper())], [live]))


def test_match_by_strm_target_when_the_strm_was_renamed() -> None:
    read = ActiveRead(read_id="r", file_name=NAME, path=".ids/4/0/c/6/b/40C6BD4C-971E")
    renamed = _session("Movie 1 (2026) - [WEBDL-2160p].strm").model_copy(
        update={"strm_target": "40c6bd4c-971e"}
    )
    assert match_reads(_server([renamed], [read]))
    assert not match_reads(_server([_session("Movie 1 (2026) - [WEBDL-2160p].strm")], [read]))


def test_match_uses_recent_files() -> None:
    recent = ActiveRead(read_id="r", file_name=NAME, path=".ids/4/0/abc", idle=True)
    snap = _server([_session("Renamed.strm").model_copy(update={"strm_target": "abc"})], [])
    idk = snap.services[1]
    assert idk.infinidysk is not None
    snap.services[1] = idk.model_copy(
        update={"infinidysk": idk.infinidysk.model_copy(update={"recent_reads": [recent]})}
    )
    assert list(match_reads(snap).values()) == [recent]


def test_no_match_without_live_feed() -> None:
    assert (
        match_reads(
            _server([_session(NAME)], [ActiveRead(read_id="r", file_name=NAME)], live=False)
        )
        == {}
    )


# ----- peers ---------------------------------------------------------------------------------
def test_redaction_strips_paths_from_messages() -> None:
    problem = ReadProblem(
        at=datetime.now(UTC),
        outcome=ProblemOutcome.FAILED,
        message=f'File "{PATH}" has missing articles: x. Path=/content/a/b.mkv at 1/2',
    )
    snap = ServiceSnapshot(
        source=_src("idk", ServiceType.INFINIDYSK),
        status=ServiceStatus.OK,
        infinidysk=InfiniDyskStats(
            reads=[ActiveRead(read_id="r", file_name=NAME, path=PATH, problems=[problem])]
        ),
    )
    stats = redact_service(snap).infinidysk
    assert stats is not None
    message = stats.reads[0].problems[0].message
    assert message == f'File "{NAME}" has missing articles: x. Path=[path] at 1/2'
