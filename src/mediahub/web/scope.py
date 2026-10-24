"""Server-scope selection (All / this server / a peer), remembered in a cookie."""

from __future__ import annotations

from fastapi import Request, Response

from mediahub.aggregation.dashboard import resolve_scope, server_options
from mediahub.context import AppContext

COOKIE = "mh_server"


def current_scope(request: Request, ctx: AppContext) -> str:
    requested = request.query_params.get("server") or request.cookies.get(COOKIE)
    return resolve_scope(ctx.monitor.store, requested)


def scope_context(request: Request, ctx: AppContext) -> dict[str, object]:
    return {
        "scope": current_scope(request, ctx),
        "server_options": server_options(ctx.monitor.store),
    }


def remember_scope(request: Request, response: Response, scope: str) -> None:
    if request.query_params.get("server") and request.cookies.get(COOKIE) != scope:
        response.set_cookie(COOKIE, scope, max_age=31_536_000, samesite="lax", httponly=True)
