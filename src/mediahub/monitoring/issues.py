"""Derive "Needs Attention" issues from snapshots. Only reports what services said."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from mediahub.models.common import ServiceStatus
from mediahub.models.downloads import ActivityEvent, ActivityKind, DownloadState
from mediahub.models.infinidysk import CircuitState
from mediahub.models.issues import Issue, IssueCategory, Severity
from mediahub.models.snapshot import ServiceSnapshot

# An import warning the *arr is still retrying only becomes an issue if it sticks around
# (e.g. a post-processor like FileFlows is still converting). Configurable per instance.
IMPORT_WARNING_GRACE_S = 600
# Failed downloads mostly leave the queue at once and show up only in history. The *arr
# usually searches again and grabs another release within moments, so a failure becomes an
# issue only when nothing newer was grabbed or imported for the same episodes/movie.
FAILED_RETRY_GRACE_S = 900
FAILED_HISTORY_MAX_AGE = timedelta(days=7)


def _fp(*parts: object) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()[:16]


def _service_link(snap: ServiceSnapshot) -> str:
    return f"/{snap.source.slug}?instance={snap.source.instance_id}"


def derive_issues(snap: ServiceSnapshot) -> list[Issue]:
    src = snap.source
    out: list[Issue] = []

    if snap.status in (ServiceStatus.UNREACHABLE, ServiceStatus.ERROR):
        category = IssueCategory.PROTOCOL
        if snap.status is ServiceStatus.UNREACHABLE:
            category = IssueCategory.UNREACHABLE
        elif snap.error and "credentials" in snap.error.lower():
            category = IssueCategory.AUTH
        verb = "unreachable" if category is IssueCategory.UNREACHABLE else "failing"
        out.append(
            Issue(
                fingerprint=_fp(src.key, "status", category),
                severity=Severity.ERROR,
                category=category,
                source=src,
                title=f"{src.instance_name} is {verb}",
                detail=snap.error,
                link=_service_link(snap),
            )
        )

    for check in snap.health_checks:
        if check.level is Severity.INFO:
            continue
        out.append(
            Issue(
                fingerprint=_fp(src.key, "health", check.source, check.message),
                severity=check.level,
                category=IssueCategory.HEALTH_CHECK,
                source=src,
                title=check.message,
                detail=check.source,
                link=check.wiki_url or _service_link(snap),
            )
        )

    warning_grace = snap.import_warning_grace_s
    if warning_grace is None:
        warning_grace = IMPORT_WARNING_GRACE_S
    for item in snap.downloads:
        if not item.state.needs_attention:
            continue
        category = {
            DownloadState.FAILED: IssueCategory.DOWNLOAD_FAILED,
            DownloadState.IMPORT_BLOCKED: IssueCategory.IMPORT_BLOCKED,
            DownloadState.IMPORT_WARNING: IssueCategory.IMPORT_BLOCKED,
        }.get(item.state, IssueCategory.DOWNLOAD_STALLED)
        lines = [
            m for msg in item.messages for m in ([msg.title] if msg.title else []) + msg.messages
        ]
        detail = item.error_message or "; ".join(lines[:3]) or None
        out.append(
            Issue(
                fingerprint=_fp(src.key, "download", item.download_id or item.item_id, item.state),
                severity=Severity.ERROR if item.state is DownloadState.FAILED else Severity.WARNING,
                category=category,
                source=src,
                title=f"{item.title}{f' · {item.subtitle}' if item.subtitle else ''}",
                detail=detail,
                link=f"/{src.slug}?instance={src.instance_id}#dl-{item.item_id}",
                grace_s=warning_grace if item.state is DownloadState.IMPORT_WARNING else 0,
            )
        )

    out.extend(_history_failures(snap))

    stats = snap.infinidysk
    if stats is not None:
        for provider in stats.providers:
            if provider.circuit is CircuitState.OPEN:
                out.append(
                    Issue(
                        fingerprint=_fp(src.key, "provider", provider.name),
                        severity=Severity.WARNING,
                        category=IssueCategory.PROVIDER,
                        source=src,
                        title=f"Provider {provider.name} circuit open",
                        detail=provider.last_failure,
                        link=_service_link(snap),
                    )
                )
            if provider.over_limit:
                out.append(
                    Issue(
                        fingerprint=_fp(src.key, "provider-limit", provider.name),
                        severity=Severity.WARNING,
                        category=IssueCategory.PROVIDER,
                        source=src,
                        title=f"Provider {provider.name} reached its byte limit",
                        detail="InfiniDysk skips this provider while it is over the limit.",
                        link=_service_link(snap),
                    )
                )
    return out


def _history_failures(snap: ServiceSnapshot) -> list[Issue]:
    src = snap.source
    cutoff = datetime.now(UTC) - FAILED_HISTORY_MAX_AGE
    queued = {media for item in snap.downloads for media in item.media_ids}
    retried_at: dict[str, datetime] = {}
    for event in snap.history:
        if event.kind in (ActivityKind.GRABBED, ActivityKind.IMPORTED):
            for media in event.media_ids:
                retried_at[media] = max(event.at, retried_at.get(media, event.at))

    def superseded(event: ActivityEvent) -> bool:
        return bool(event.media_ids) and all(
            media in queued or (media in retried_at and retried_at[media] > event.at)
            for media in event.media_ids
        )

    out: list[Issue] = []
    for event in snap.history:
        if event.kind is not ActivityKind.FAILED or event.at < cutoff or superseded(event):
            continue
        out.append(
            Issue(
                fingerprint=_fp(src.key, "history-failed", event.download_id or event.event_id),
                severity=Severity.WARNING,
                category=IssueCategory.DOWNLOAD_FAILED,
                source=src,
                title=f"{event.title}{f' · {event.subtitle}' if event.subtitle else ''}",
                detail=event.detail,
                first_seen=event.at,
                link=_service_link(snap),
                grace_s=FAILED_RETRY_GRACE_S,
            )
        )
    return out
