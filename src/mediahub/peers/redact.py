"""Strip data that must never leave this server: client IPs, file paths, internal URLs.

Applied to every snapshot served to a peer (``/peer/v1``). Credentials never enter
snapshots in the first place; this removes the remaining sensitive-but-useful-locally fields.
Upstream messages (download warnings, history details, health checks, issues) are kept but
any absolute path in them is replaced with ``[path]``, e.g. *arr remote-path-mapping warnings.
"""

from __future__ import annotations

import re

from mediahub.models.downloads import ActivityEvent, DownloadItem, StatusMessage
from mediahub.models.infinidysk import ActiveRead, InfiniDyskStats
from mediahub.models.issues import Issue
from mediahub.models.snapshot import HealthCheck, ServerSnapshot, ServiceSnapshot

# Absolute paths inside log text: a "/" after the start, whitespace or a quote, and at least
# two segments (so "1/2" or "MB/s" survive). Quoted paths may contain spaces.
_QUOTED_PATH = re.compile(r'"/[^"]*/[^"]*"')
_BARE_PATH = re.compile(r"(?<![\w.:/])/[\w.\-]+(?:/[\w.\-]+)+")


def redact_text(text: str, path: str | None = None, file_name: str | None = None) -> str:
    if path:
        text = text.replace(path, file_name or "[path]")
    text = _QUOTED_PATH.sub('"[path]"', text)
    return _BARE_PATH.sub("[path]", text)


def _opt(text: str | None) -> str | None:
    return redact_text(text) if text else text


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
            "feed_error": _opt(stats.feed_error),
        }
    )


def _redact_download(item: DownloadItem) -> DownloadItem:
    messages = [
        StatusMessage(title=_opt(m.title), messages=[redact_text(t) for t in m.messages])
        for m in item.messages
    ]
    return item.model_copy(update={"messages": messages, "error_message": _opt(item.error_message)})


def _redact_event(event: ActivityEvent) -> ActivityEvent:
    return event.model_copy(update={"detail": _opt(event.detail)})


def _redact_check(check: HealthCheck) -> HealthCheck:
    return check.model_copy(update={"message": redact_text(check.message)})


def _redact_issue(issue: Issue) -> Issue:
    return issue.model_copy(
        update={"title": redact_text(issue.title), "detail": _opt(issue.detail)}
    )


def redact_service(snapshot: ServiceSnapshot) -> ServiceSnapshot:
    return snapshot.model_copy(
        update={
            "error": _opt(snapshot.error),
            "health_checks": [_redact_check(c) for c in snapshot.health_checks],
            "sessions": [s.model_copy(update={"remote_address": None}) for s in snapshot.sessions],
            "downloads": [_redact_download(d) for d in snapshot.downloads],
            "history": [_redact_event(e) for e in snapshot.history],
            "infinidysk": _redact_infinidysk(snapshot.infinidysk),
            "issues": [_redact_issue(i) for i in snapshot.issues],
        }
    )


def redact_server(snapshot: ServerSnapshot) -> ServerSnapshot:
    return snapshot.model_copy(update={"services": [redact_service(s) for s in snapshot.services]})
