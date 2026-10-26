"""Peer access tokens (grants): issued here, presented by a peer as ``Authorization: Bearer``.

Only a SHA-256 of the token is stored, plus a short prefix so the user can tell tokens apart.
The full token is shown once, when it's created.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediahub.db.models import PeerGrant

TOKEN_PREFIX = "mhp_"  # noqa: S105 - a format marker, not a secret
SHOWN_PREFIX_LEN = len(TOKEN_PREFIX) + 6
# Don't write last_used_at on every poll; this is precise enough for "last used 3 min ago".
TOUCH_EVERY_S = 60


@dataclass(frozen=True)
class GrantInfo:
    id: int
    label: str
    scopes: frozenset[str]


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX) and 20 <= len(value) <= 128


def create_grant(db: Session, label: str) -> tuple[PeerGrant, str]:
    token = generate_token()
    grant = PeerGrant(
        label=label,
        token_hash=hash_token(token),
        token_prefix=token[:SHOWN_PREFIX_LEN],
        scopes="read",
    )
    db.add(grant)
    db.flush()
    return grant, token


def list_grants(db: Session) -> list[PeerGrant]:
    return list(db.scalars(select(PeerGrant).order_by(PeerGrant.created_at.desc())))


def revoke_grant(db: Session, grant_id: int) -> PeerGrant | None:
    grant = db.get(PeerGrant, grant_id)
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
    return grant


def delete_grant(db: Session, grant_id: int) -> bool:
    grant = db.get(PeerGrant, grant_id)
    if grant is None or grant.revoked_at is None:
        return False  # only revoked tokens can be deleted, so the list shows what was cut off
    db.delete(grant)
    return True


def authenticate(db: Session, token: str, ip: str | None, now: datetime) -> GrantInfo | None:
    if not looks_like_token(token):
        return None
    grant = db.scalar(select(PeerGrant).where(PeerGrant.token_hash == hash_token(token)))
    if grant is None or grant.revoked_at is not None:
        return None
    last = grant.last_used_at
    if last is None or (now - last).total_seconds() >= TOUCH_EVERY_S or grant.last_used_ip != ip:
        grant.last_used_at = now
        grant.last_used_ip = ip
    return GrantInfo(grant.id, grant.label, frozenset(grant.scopes.split(",")))
