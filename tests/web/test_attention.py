"""Acknowledging and muting issues on Needs Attention."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck, ServiceSnapshot

from .conftest import csrf_from


def _put_health(client: TestClient, *messages: str) -> str:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    source = SourceRef(
        server_id=ctx.identity.server_id,
        server_name=ctx.identity.server_name,
        instance_id="sonarr-1",
        instance_name="TV",
        service_type=ServiceType.SONARR,
    )
    checks = [HealthCheck(level=Severity.WARNING, message=m) for m in messages]
    ctx.monitor.store.put_local(
        ServiceSnapshot(source=source, status=ServiceStatus.OK, health_checks=checks)
    )
    return str(ctx.monitor.store.get_local("sonarr-1").issues[0].fingerprint)


def _post(client: TestClient, token: str, **data: str) -> str:
    resp = client.post("/attention/issues?server=all", data=data, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200, resp.text
    return resp.text


def test_acknowledge_mute_and_restore(authed: TestClient) -> None:
    fingerprint = _put_health(authed, "Indexers are unavailable")
    page = authed.get("/attention")
    token = csrf_from(page.text)
    assert "Indexers are unavailable" in page.text and "Acknowledge" in page.text

    html = _post(authed, token, fingerprint=fingerprint, action="ack")
    assert "All clear" in html
    assert "Acknowledged &amp; muted" in html and "shows again if it recurs" in html
    assert "Show again" in html
    assert authed.get("/partials/dashboard/attention?server=all").text.strip() == ""

    html = _post(authed, token, fingerprint=fingerprint, action="restore")
    assert "All clear" not in html and "Acknowledged &amp; muted" not in html

    html = _post(authed, token, fingerprint=fingerprint, action="mute", hours="24")
    assert "Muted · shows again in 23h" in html or "Muted · shows again in 1d" in html


def test_bad_requests(authed: TestClient) -> None:
    fingerprint = _put_health(authed, "Disk space low")
    token = csrf_from(authed.get("/attention").text)
    bad_hours = authed.post(
        "/attention/issues",
        data={"fingerprint": fingerprint, "action": "mute", "hours": "5"},
        headers={"X-CSRF-Token": token},
    )
    assert bad_hours.status_code == 400
    no_csrf = authed.post("/attention/issues", data={"fingerprint": fingerprint, "action": "ack"})
    assert no_csrf.status_code == 403
    # Unknown (e.g. already resolved) issues just re-render the list.
    assert "Disk space low" in _post(authed, token, fingerprint="gone", action="ack")
