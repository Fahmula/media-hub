"""Reads another MediaHub's ``/peer/v1`` API and turns its snapshot into local view data.

A peer's snapshot is validated against our models (size-limited, schema-versioned), and
rewritten so it is safe and usable here: every source is marked remote and labelled with the
peer's local name, services claiming to come from another server are dropped (no relaying),
image URLs point at our own ``/img/peer/...`` proxy, and links that could leave the app are
removed.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from mediahub.integrations.base import ConnectionTestResult
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import make_client, request
from mediahub.models.snapshot import SCHEMA_VERSION, ServerSnapshot
from mediahub.peers.repository import PeerConfig

SNAPSHOT_PATH = "/peer/v1/snapshot"
IMAGE_PATH = "/peer/v1/img"
# Sent only while a user is watching this server, so the peer polls its services fast too.
DEMAND_HEADER = "X-MediaHub-Demand"
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
TIMEOUT = httpx.Timeout(10.0, connect=4.0)

_SIGNED_IMAGE = re.compile(r"/img/([A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64}){3})")
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_SUFFIXES = (".ts.net", ".local", ".lan", ".home.arpa", ".internal", ".localdomain")


def insecure_transport(base_url: str) -> bool:
    """Plain HTTP to something that isn't Tailscale, a LAN address or a local host name."""
    parts = urlsplit(base_url)
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not host.endswith(_PRIVATE_SUFFIXES)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr in _TAILSCALE_NET)


def _is_source(node: dict[str, Any]) -> bool:
    return {"server_id", "server_name", "instance_id", "instance_name"} <= node.keys()


def localize(node: Any, peer_id: str, peer_name: str) -> Any:
    """Rewrite a raw snapshot (JSON) in place for use on this server."""
    if isinstance(node, list):
        return [localize(item, peer_id, peer_name) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "image_url":
            out[key] = _local_image_url(value, peer_id)
        elif key == "link":
            ok = isinstance(value, str) and value.startswith("/") and not value.startswith("//")
            out[key] = value if ok else None
        elif key == "wiki_url":
            ok = isinstance(value, str) and value.startswith(("https://", "http://"))
            out[key] = value if ok else None
        else:
            out[key] = localize(value, peer_id, peer_name)
    if _is_source(out):
        out["is_remote"] = True
        out["server_name"] = peer_name
    return out


def _local_image_url(value: Any, peer_id: str) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("/img/demo/"):
        return value  # generated placeholder posters, served by every MediaHub
    match = _SIGNED_IMAGE.fullmatch(value)
    return f"/img/peer/{peer_id}/{match.group(1)}" if match else None


def parse_snapshot(
    raw: Any,
    config: PeerConfig,
    local_server_id: str,
    expected_server_id: str | None = None,
) -> ServerSnapshot:
    if not isinstance(raw, dict) or "server_id" not in raw:
        raise IntegrationError(
            ErrorKind.PROTOCOL, "Not a MediaHub peer snapshot — check the peer URL"
        )
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise IntegrationError(
            ErrorKind.PROTOCOL,
            f"Incompatible peer: it sends snapshot schema v{version}, this server reads "
            f"v{SCHEMA_VERSION}. Update both MediaHub installs to the same version.",
        )
    server_id = raw.get("server_id")
    if server_id == local_server_id:
        raise IntegrationError(
            ErrorKind.PROTOCOL, "This URL points back at this MediaHub server, not a peer"
        )
    if expected_server_id and server_id != expected_server_id:
        raise IntegrationError(
            ErrorKind.PROTOCOL,
            f"This URL now answers as a different MediaHub (server ID {server_id}). If that's "
            "expected, edit the peer and save it again.",
        )
    services = raw.get("services")
    if isinstance(services, list):
        raw = {
            **raw,
            "services": [
                s
                for s in services
                if isinstance(s, dict)
                and isinstance(s.get("source"), dict)
                and s["source"].get("server_id") == server_id
            ],
        }
    try:
        snapshot = ServerSnapshot.model_validate(localize(raw, config.id, config.name))
    except ValidationError as exc:
        raise IntegrationError(
            ErrorKind.PROTOCOL,
            f"Incompatible peer: its snapshot doesn't match this version "
            f"({exc.error_count()} invalid fields). Update both MediaHub installs.",
        ) from exc
    return snapshot.model_copy(update={"is_local": False})


class PeerClient:
    def __init__(self, config: PeerConfig, local_server_id: str) -> None:
        self.config = config
        self._local_server_id = local_server_id
        # The server first seen at this URL; a different one answering later is an error.
        self.expected_server_id = config.remote_server_id
        self._client = make_client(
            config.base_url, headers={"Authorization": f"Bearer {config.token}"}, timeout=TIMEOUT
        )

    async def fetch_snapshot(self, *, active: bool) -> ServerSnapshot:
        headers = {DEMAND_HEADER: "active"} if active else {}
        try:
            response = await request(self._client, "GET", SNAPSHOT_PATH, headers=headers)
        except IntegrationError as exc:
            if exc.kind is ErrorKind.AUTH:
                raise IntegrationError(
                    ErrorKind.AUTH, f"{exc.message}: the access token is wrong or was revoked"
                ) from exc
            raise
        if len(response.content) > MAX_SNAPSHOT_BYTES:
            raise IntegrationError(ErrorKind.PROTOCOL, "The peer's snapshot is too large")
        try:
            raw = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IntegrationError(
                ErrorKind.PROTOCOL, "Expected JSON — is this URL a MediaHub server?"
            ) from exc
        return parse_snapshot(raw, self.config, self._local_server_id, self.expected_server_id)

    async def fetch_image(self, ref: str) -> tuple[bytes, str] | None:
        """``ref`` is ``instance/item/tag/signature``, as signed by the peer."""
        try:
            response = await request(self._client, "GET", f"{IMAGE_PATH}/{ref}")
        except IntegrationError:
            return None
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if len(response.content) > MAX_IMAGE_BYTES:
            return None
        return response.content, content_type

    async def test(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=False)
        try:
            snapshot = await self.fetch_snapshot(active=False)
        except IntegrationError as exc:
            label = {
                ErrorKind.UNREACHABLE: "Reach the peer",
                ErrorKind.AUTH: "Access token",
            }.get(exc.kind, "Read the peer's data")
            result.add(label, False, exc.message)
            return result
        result.ok = True
        result.server_name = snapshot.server_name
        result.version = snapshot.app_version
        result.add("Reach the peer", True, f"MediaHub {snapshot.app_version or ''}".strip())
        result.add("Access token", True, "Accepted (read-only)")
        n = len(snapshot.services)
        result.add("Shared services", True, f"{n} service{'s' if n != 1 else ''}")
        if insecure_transport(self.config.base_url):
            result.add(
                "Transport",
                False,
                "Plain HTTP outside Tailscale or a private network: the token could be "
                "intercepted. Prefer https:// (e.g. tailscale serve).",
            )
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
