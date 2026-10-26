"""Parsing and validation of the "link a server" form and the access-token label.

The peer's token is write-only like service secrets: blank on edit keeps the stored one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mediahub.instances.urls import InvalidURLError, normalize_base_url
from mediahub.peers.repository import PeerConfig
from mediahub.peers.tokens import looks_like_token

NAME_MAX = 64
LABEL_MAX = 64
BAD_TOKEN = "That isn't a MediaHub access token (they start with mhp_)."  # noqa: S105
MISSING_TOKEN = "Paste the access token created on the other server."  # noqa: S105


@dataclass
class PeerForm:
    name: str = ""
    base_url: str = ""
    enabled: bool = True
    new_token: str | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_config(self, peer_id: str, stored_token: str | None) -> PeerConfig:
        return PeerConfig(
            id=peer_id,
            name=self.name or "Test",
            base_url=self.base_url,
            token=self.new_token or stored_token or "",
            enabled=self.enabled,
        )


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def parse_peer_form(data: Mapping[str, Any], *, has_stored_token: bool = False) -> PeerForm:
    form = PeerForm()
    form.name = _text(data, "name")
    if not form.name:
        form.errors["name"] = "Give this server a name, e.g. “Brother Server”."
    elif len(form.name) > NAME_MAX:
        form.errors["name"] = f"Keep the name under {NAME_MAX} characters."

    form.base_url = _text(data, "base_url")
    try:
        form.base_url = normalize_base_url(form.base_url)
    except InvalidURLError as exc:
        form.errors["base_url"] = str(exc)

    token = _text(data, "token")
    if token:
        if looks_like_token(token) and not any(c.isspace() for c in token):
            form.new_token = token
        else:
            form.errors["token"] = BAD_TOKEN
    elif not has_stored_token:
        form.errors["token"] = MISSING_TOKEN
    form.enabled = data.get("enabled") == "on"
    return form


def parse_grant_label(data: Mapping[str, Any]) -> tuple[str, str | None]:
    label = _text(data, "label")
    if not label:
        return label, "Name the server that will use this token, e.g. “Skynet”."
    if len(label) > LABEL_MAX:
        return label, f"Keep the name under {LABEL_MAX} characters."
    return label, None
