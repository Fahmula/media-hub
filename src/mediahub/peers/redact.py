"""Strip data that must never leave this server: client IPs, file paths, internal URLs.

Applied to every snapshot served to a peer (``/peer/v1``). Credentials never enter
snapshots in the first place; this removes the remaining sensitive-but-useful-locally fields.
"""

from __future__ import annotations

from mediahub.models.infinidysk import InfiniDyskStats
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot


def _redact_infinidysk(stats: InfiniDyskStats | None) -> InfiniDyskStats | None:
    if stats is None:
        return None
    reads = [r.model_copy(update={"path": None, "client_ip": None}) for r in stats.reads]
    return stats.model_copy(update={"reads": reads})


def redact_service(snapshot: ServiceSnapshot) -> ServiceSnapshot:
    return snapshot.model_copy(
        update={
            "sessions": [s.model_copy(update={"remote_address": None}) for s in snapshot.sessions],
            "infinidysk": _redact_infinidysk(snapshot.infinidysk),
        }
    )


def redact_server(snapshot: ServerSnapshot) -> ServerSnapshot:
    return snapshot.model_copy(update={"services": [redact_service(s) for s in snapshot.services]})
