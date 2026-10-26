"""Peer federation: tokens, snapshot validation and rewriting, redaction, demand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mediahub.aggregation.dashboard import SCOPE_ALL, build_view
from mediahub.db.app_settings import ServerIdentity
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.integrations.errors import IntegrationError
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import ActivityEvent, ActivityKind, DownloadItem, StatusMessage
from mediahub.models.issues import Severity
from mediahub.models.playback import MediaInfo, PlaybackSession
from mediahub.models.snapshot import HealthCheck, ServerSnapshot, ServiceSnapshot
from mediahub.monitoring.demand import DemandTracker
from mediahub.monitoring.issue_states import IssueStates
from mediahub.monitoring.issues import derive_issues
from mediahub.monitoring.store import SnapshotStore
from mediahub.peers import tokens
from mediahub.peers.client import insecure_transport, parse_snapshot
from mediahub.peers.redact import redact_server
from mediahub.peers.repository import PeerConfig

NOW = datetime.now(UTC)
LOCAL_ID = "00000000-0000-4000-8000-00000000000a"
REMOTE_ID = "00000000-0000-4000-8000-00000000000b"
PEER = PeerConfig(id="peer-1", name="Brother", base_url="https://b.ts.net", token="mhp_x")


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_migrations(engine)
    return make_sessionmaker(engine)


def remote_source(instance: str = "r1", server_id: str = REMOTE_ID) -> SourceRef:
    return SourceRef(
        server_id=server_id,
        server_name="Brother Server",
        instance_id=instance,
        instance_name="TV",
        service_type=ServiceType.SONARR,
    )


def remote_snapshot(**overrides: Any) -> dict[str, Any]:
    src = remote_source()
    service = ServiceSnapshot(
        source=src,
        status=ServiceStatus.OK,
        fetched_at=NOW,
        downloads=[
            DownloadItem(
                source=src, item_id="1", title="Show", image_url="/img/inst/42/poster/abcDEF_-1"
            )
        ],
        health_checks=[HealthCheck(level=Severity.WARNING, message="x", wiki_url="javascript:x")],
    )
    raw = ServerSnapshot(
        server_id=REMOTE_ID,
        server_name="Brother Server",
        generated_at=NOW,
        services=[service],
        app_version="0.3.0",
    ).model_dump(mode="json")
    raw.update(overrides)
    return raw


# ----- tokens --------------------------------------------------------------------------------
def test_grant_tokens_are_stored_hashed_and_revocable(sessions: sessionmaker[Session]) -> None:
    with session_scope(sessions) as db:
        grant, token = tokens.create_grant(db, "Skynet")
        assert token.startswith("mhp_") and len(token) > 40
        assert grant.token_hash == tokens.hash_token(token) and token not in grant.token_hash
        assert grant.token_prefix == token[: tokens.SHOWN_PREFIX_LEN]
        grant_id = grant.id

    with session_scope(sessions) as db:
        info = tokens.authenticate(db, token, "100.64.0.2", NOW)
        assert info is not None and info.label == "Skynet" and "read" in info.scopes
        assert tokens.authenticate(db, token + "x", None, NOW) is None
        assert tokens.authenticate(db, "Bearer nonsense", None, NOW) is None
        assert tokens.delete_grant(db, grant_id) is False  # active tokens are revoked first
        tokens.revoke_grant(db, grant_id)
    with session_scope(sessions) as db:
        assert tokens.authenticate(db, token, None, NOW) is None
        assert tokens.delete_grant(db, grant_id) is True


def test_last_used_is_written_at_most_once_a_minute(sessions: sessionmaker[Session]) -> None:
    with session_scope(sessions) as db:
        grant, token = tokens.create_grant(db, "Skynet")
        tokens.authenticate(db, token, "ip", NOW)
        tokens.authenticate(db, token, "ip", NOW + timedelta(seconds=30))
        assert grant.last_used_at == NOW
        tokens.authenticate(db, token, "ip", NOW + timedelta(seconds=61))
        assert grant.last_used_at == NOW + timedelta(seconds=61)


# ----- transport ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "insecure"),
    [
        ("https://example.com", False),
        ("http://mediahub.tail1234.ts.net", False),
        ("http://100.101.1.2:8080", False),
        ("http://192.168.1.10:8080", False),
        ("http://brother-server:8080", False),  # MagicDNS short name / LAN host
        ("http://unraid.local", False),
        ("http://example.com", True),
        ("http://8.8.8.8", True),
    ],
)
def test_insecure_transport(url: str, insecure: bool) -> None:
    assert insecure_transport(url) is insecure


# ----- parsing --------------------------------------------------------------------------------
def test_snapshot_is_rewritten_for_local_use() -> None:
    snap = parse_snapshot(remote_snapshot(), PEER, LOCAL_ID)
    assert snap.is_local is False and snap.server_name == "Brother Server"
    service = snap.services[0]
    assert service.source.is_remote and service.source.server_name == "Brother"
    download = service.downloads[0]
    assert download.source.is_remote
    assert download.image_url == "/img/peer/peer-1/inst/42/poster/abcDEF_-1"
    assert service.health_checks[0].wiki_url is None


def test_unexpected_image_urls_are_dropped() -> None:
    raw = remote_snapshot()
    raw["services"][0]["downloads"][0]["image_url"] = "https://evil.example/x.png"
    assert parse_snapshot(raw, PEER, LOCAL_ID).services[0].downloads[0].image_url is None
    raw["services"][0]["downloads"][0]["image_url"] = "/img/demo/x.svg?t=Show"
    assert parse_snapshot(raw, PEER, LOCAL_ID).services[0].downloads[0].image_url == (
        "/img/demo/x.svg?t=Show"
    )


def test_relayed_services_are_dropped() -> None:
    raw = remote_snapshot()
    other = ServiceSnapshot(source=remote_source("x", server_id="someone-else"))
    raw["services"].append(other.model_dump(mode="json"))
    assert [s.source.instance_id for s in parse_snapshot(raw, PEER, LOCAL_ID).services] == ["r1"]


@pytest.mark.parametrize(
    ("raw", "expected", "message"),
    [
        (remote_snapshot(schema_version=99), None, "Incompatible peer"),
        (remote_snapshot(server_id=LOCAL_ID), None, "points back at this MediaHub"),
        (remote_snapshot(), "another-id", "answers as a different MediaHub"),
        ({"hello": "world"}, None, "Not a MediaHub peer"),
        (remote_snapshot(generated_at="yesterday"), None, "Incompatible peer"),
    ],
)
def test_bad_snapshots_are_rejected(raw: Any, expected: str | None, message: str) -> None:
    with pytest.raises(IntegrationError, match=message):
        parse_snapshot(raw, PEER, LOCAL_ID, expected)


# ----- redaction ------------------------------------------------------------------------------
def test_redaction_removes_paths_from_messages_and_ips() -> None:
    src = remote_source()
    path_msg = "Import failed, path does not exist: /data/downloads/Show.S01E01"
    service = ServiceSnapshot(
        source=src,
        error="Couldn't read /config/thing.db",
        health_checks=[HealthCheck(level=Severity.WARNING, message=path_msg)],
        sessions=[
            PlaybackSession(
                source=src, session_id="s", remote_address="10.0.0.5", media=MediaInfo(title="X")
            )
        ],
        downloads=[
            DownloadItem(
                source=src,
                item_id="1",
                title="Show",
                messages=[StatusMessage(title="/data/x/Show.mkv", messages=[path_msg])],
                error_message=path_msg,
            )
        ],
        history=[
            ActivityEvent(
                source=src,
                event_id="1",
                kind=ActivityKind.FAILED,
                title="Show",
                at=NOW,
                detail=path_msg,
            )
        ],
    )
    server = ServerSnapshot(server_id=REMOTE_ID, server_name="B", generated_at=NOW)
    out = redact_server(server.model_copy(update={"services": [service]}))
    text = out.model_dump_json()
    assert "/data/" not in text and "/config/" not in text and "10.0.0.5" not in text
    assert "[path]" in out.services[0].downloads[0].messages[0].messages[0]


# ----- demand and store -----------------------------------------------------------------------
def test_remote_demand_does_not_count_as_a_local_viewer() -> None:
    demand = DemandTracker()
    demand.touch_remote()
    assert demand.is_active() and not demand.is_user_active()
    demand.touch()
    assert demand.is_user_active()


async def test_peer_snapshot_resolves_acknowledged_remote_issues(
    sessions: sessionmaker[Session],
) -> None:
    states = IssueStates(sessions)
    store = SnapshotStore(
        identity=ServerIdentity(server_id=LOCAL_ID, server_name="Skynet"), issue_states=states
    )
    raw = remote_snapshot()
    raw["services"][0]["health_checks"] = [{"level": "warning", "message": "Indexer down"}]
    # The issue travels in the snapshot: the peer derived it.
    with_issue = parse_snapshot(raw, PEER, LOCAL_ID)
    issues = derive_issues(with_issue.services[0])
    service = with_issue.services[0].model_copy(update={"issues": issues})
    snap = with_issue.model_copy(update={"services": [service]})

    state = store.peer(PEER.id, PEER.name)
    store.put_peer(state, snap, NOW)
    view = build_view(store, SCOPE_ALL)
    assert [i.title for i in view.issues] == ["Indexer down"]
    await states.acknowledge(view.issues[0])
    assert build_view(store, SCOPE_ALL).issues == []

    cleared = snap.model_copy(update={"services": [service.model_copy(update={"issues": []})]})
    store.put_peer(state, cleared, NOW)
    await states.aclose()
    assert states.get(issues[0].fingerprint) is None


def test_the_same_server_linked_twice_is_detected() -> None:
    store = SnapshotStore(identity=ServerIdentity(server_id=LOCAL_ID, server_name="Skynet"))
    snap = parse_snapshot(remote_snapshot(), PEER, LOCAL_ID)
    store.put_peer(store.peer("a", "A"), snap, NOW)
    assert store.peer_linked_as(REMOTE_ID, exclude="b") is store.peer("a", "A")
    assert store.peer_linked_as(REMOTE_ID, exclude="a") is None


def test_a_down_peer_keeps_its_last_data_but_marks_it_stale() -> None:
    store = SnapshotStore(identity=ServerIdentity(server_id=LOCAL_ID, server_name="Skynet"))
    state = store.peer(PEER.id, PEER.name)
    store.put_peer(state, parse_snapshot(remote_snapshot(), PEER, LOCAL_ID), NOW)
    card = build_view(store, SCOPE_ALL).servers[1]
    assert card.services_up == 1 and not card.services[0].stale

    state.status, state.error = ServiceStatus.UNREACHABLE, "Could not connect"
    view = build_view(store, SCOPE_ALL)
    assert view.servers[1].services_up == 0 and view.servers[1].services[0].stale
    assert [s.source.instance_id for s in view.snapshots] == ["r1"]  # still shown

    state.last_success_at = NOW - timedelta(minutes=11)
    assert build_view(store, SCOPE_ALL).snapshots == []
