"""``/peer/v1``: what a linked MediaHub may read from this server.

Token-authenticated (``Authorization: Bearer mhp_…``, see ``peers/tokens.py``), read-only,
no cookies and no CSRF. Only this server's own instances are served (never data from its
peers), always passed through ``redact_server`` first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from mediahub import __version__
from mediahub.context import AppContext, get_context
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.peers import tokens
from mediahub.peers.client import DEMAND_HEADER
from mediahub.peers.redact import redact_server
from mediahub.security.ratelimit import RateLimiter
from mediahub.web.routes.auth import client_ip
from mediahub.web.routes.images import serve_signed_image

log = get_logger(__name__)

failed_auth = RateLimiter(limit=20, window_s=600)
# A peer polls every 10 s while watched; the rest of the budget is for posters.
grant_limiter = RateLimiter(limit=300, window_s=60)


async def require_grant(
    request: Request, ctx: AppContext = Depends(get_context)
) -> tokens.GrantInfo:
    ip = client_ip(request)
    if failed_auth.blocked(ip):
        raise HTTPException(429, "Too many failed attempts; try again later")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()

    def check() -> tokens.GrantInfo | None:
        with session_scope(ctx.sessions) as db:
            return tokens.authenticate(db, token, ip, datetime.now(UTC))

    grant = await run_in_threadpool(check) if scheme.lower() == "bearer" and token else None
    if grant is None:
        failed_auth.hit(ip)
        log.warning("peer_auth_failed", ip=ip)
        raise HTTPException(401, "Missing, invalid or revoked access token")
    if "read" not in grant.scopes:
        raise HTTPException(403, "This token can't read this server")
    if grant_limiter.blocked(str(grant.id)):
        raise HTTPException(429, "Too many requests")
    grant_limiter.hit(str(grant.id))
    return grant


router = APIRouter(prefix="/peer/v1", dependencies=[Depends(require_grant)])


@router.get("/snapshot")
async def snapshot(request: Request, ctx: AppContext = Depends(get_context)) -> Response:
    if request.headers.get(DEMAND_HEADER) == "active":
        ctx.monitor.demand.touch_remote()
    snap = redact_server(ctx.monitor.store.local_server()).model_copy(
        update={"app_version": __version__}
    )
    return Response(
        snap.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/img/{instance_id}/{item_id}/{tag}/{signature}")
async def image(
    instance_id: str,
    item_id: str,
    tag: str,
    signature: str,
    ctx: AppContext = Depends(get_context),
) -> Response:
    return await serve_signed_image(ctx, instance_id, item_id, tag, signature)
