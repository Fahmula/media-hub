"""Maps service types to integration classes."""

from __future__ import annotations

from mediahub.integrations.base import Integration
from mediahub.models.common import ServiceType


def integration_class(service_type: ServiceType) -> type[Integration]:
    # Imported lazily so integrations can depend on shared modules without import cycles.
    from mediahub.integrations.arr.integration import RadarrIntegration, SonarrIntegration
    from mediahub.integrations.emby.integration import EmbyIntegration
    from mediahub.integrations.infinidysk.integration import InfiniDyskIntegration

    return {
        ServiceType.EMBY: EmbyIntegration,
        ServiceType.SONARR: SonarrIntegration,
        ServiceType.RADARR: RadarrIntegration,
        ServiceType.INFINIDYSK: InfiniDyskIntegration,
    }[service_type]
