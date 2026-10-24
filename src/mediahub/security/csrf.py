"""CSRF protection (synchronizer token).

Logged-in requests use the token stored with the server-side session. Anonymous forms
(login, first-run setup) use a double-submit cookie. htmx sends the token as
``X-CSRF-Token`` (configured on ``<body hx-headers>``); plain forms post ``csrf_token``.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

HEADER = "x-csrf-token"
FORM_FIELD = "csrf_token"
PRE_COOKIE = "mh_csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def expected_token(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return auth.csrf_token
    token = getattr(request.state, "pre_csrf", None) or request.cookies.get(PRE_COOKIE)
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.state.new_pre_csrf = token
    request.state.pre_csrf = token
    return token


async def verify_csrf(request: Request) -> None:
    """Router-level dependency: rejects state-changing requests without a valid token."""
    if request.method in SAFE_METHODS:
        return
    submitted = request.headers.get(HEADER)
    if not submitted:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(FORM_FIELD)
            submitted = value if isinstance(value, str) else None
    auth = getattr(request.state, "auth", None)
    expected = auth.csrf_token if auth is not None else request.cookies.get(PRE_COOKIE)
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
