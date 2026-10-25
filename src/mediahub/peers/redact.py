"""Strip data that must never leave this server: client IPs, file paths, internal URLs.

Applied to every snapshot served to a peer (``/peer/v1``). Credentials never enter
snapshots in the first place; this removes the remaining sensitive-but-useful-locally fields.
"""

from __future__ import annotations

import re

from mediahub.models.infinidysk import ActiveRead, InfiniDyskStats
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot

# Absolute paths inside log text: a "/" after the start, whitespace or a quote, and at least
# two segments (so "1/2" or "MB/s" survive). Quoted paths may contain spaces.
_QUOTED_PATH = re.compile(r'"/[^"]*/[^"]*"')
_BARE_PATH = re.compile(r"(?<![\w.:/])/[\w.\-]+(?:/[\w.\-]+)+")


def redact_text(text: str, path: str | None = None, file_name: str | None = None) -> str:
    if path:
        text = text.replace(path, file_name or "[path]")
    text = _QUOTED_PATH.sub('"[path]"', text)
    return _BARE_PATH.sub("[path]", text)


def _redact_read(read: ActiveRead) -> ActiveRead:
    problems = [
        p.model_copy(update={"message": redact_text(p.message, read.path, read.file_name)})
        for p in read.problems
    ]
    return read.model_copy(update={"path": None, "client_ip": None, "problems": problems})


def _redact_infinidysk(stats: InfiniDyskStats | None) -> InfiniDyskStats | None:
    if stats is None:
        return None
    return stats.model_copy(
        update={
            "reads": [_redact_read(r) for r in stats.reads],
            "recent_reads": [_redact_read(r) for r in stats.recent_reads],
        }
    )


def redact_service(snapshot: ServiceSnapshot) -> ServiceSnapshot:
    return snapshot.model_copy(
        update={
            "sessions": [s.model_copy(update={"remote_address": None}) for s in snapshot.sessions],
            "infinidysk": _redact_infinidysk(snapshot.infinidysk),
        }
    )


def redact_server(snapshot: ServerSnapshot) -> ServerSnapshot:
    return snapshot.model_copy(update={"services": [redact_service(s) for s in snapshot.services]})
