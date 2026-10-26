"""Security headers and request logging as a pure ASGI middleware."""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mediahub.logging import get_logger

log = get_logger("mediahub.http")

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",  # progress widths; style attributes can't run script
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]

_QUIET_PATHS = ("/healthz", "/static/", "/sw.js", "/manifest.webmanifest")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend(h for h in _HEADERS if h[0] not in present)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path: str = scope.get("path", "")
            if not path.startswith(_QUIET_PATHS) or status >= 400:
                log.info(
                    "request",
                    method=scope.get("method"),
                    path=path,  # never the query string: it may carry tokens
                    status=status,
                    ms=round((time.perf_counter() - start) * 1000, 1),
                )
