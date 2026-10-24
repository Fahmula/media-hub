"""Jinja2 environment and the ``render`` helper used by every HTML route."""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mediahub import __version__
from mediahub.models.common import ServiceType
from mediahub.security import csrf
from mediahub.web import filters
from mediahub.web.icons import icon

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(icon=icon, app_version=__version__, ServiceType=ServiceType)
templates.env.filters.update(filters.FILTERS)


@cache
def _asset_hash(path: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    except OSError:
        return __version__


def static_url(path: str) -> str:
    return f"/static/{path}?v={_asset_hash(path)}"


templates.env.globals["static_url"] = static_url


THEMES = {"dark": "mediahub", "light": "mediahub-light"}


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    ctx = request.app.state.ctx
    base: dict[str, Any] = {
        "auth": getattr(request.state, "auth", None),
        "csrf_token": csrf.expected_token(request),
        "identity": ctx.identity,
        "demo_mode": ctx.settings.demo_mode,
        "current_path": request.url.path,
        "is_htmx": is_htmx(request),
        "theme": THEMES.get(request.cookies.get("mh_theme", ""), "mediahub"),
    }
    base.update(context or {})
    response = templates.TemplateResponse(
        request, name, base, status_code=status_code, headers=headers
    )
    new_pre = getattr(request.state, "new_pre_csrf", None)
    if new_pre:
        response.set_cookie(
            csrf.PRE_COOKIE,
            new_pre,
            httponly=True,
            samesite="strict",
            secure=cookie_secure(request),
            max_age=60 * 60 * 12,
        )
    return response


def cookie_secure(request: Request) -> bool:
    mode = request.app.state.ctx.settings.cookie_secure
    if mode == "auto":
        return request.url.scheme == "https"
    return mode == "true"
