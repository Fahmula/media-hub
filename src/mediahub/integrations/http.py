"""Shared HTTP plumbing for integrations: client construction and error translation."""

from __future__ import annotations

import json
from typing import Any

import httpx

from mediahub import __version__
from mediahub.integrations.errors import ErrorKind, IntegrationError

DEFAULT_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
USER_AGENT = f"MediaHub/{__version__}"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


def make_client(
    base_url: str,
    *,
    headers: dict[str, str] | None = None,
    verify_tls: bool = True,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
        timeout=timeout,
        follow_redirects=False,  # a redirect usually means a wrong base URL; never follow blindly
        verify=verify_tls,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


def _describe_transport_error(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "Connection timed out"
    if isinstance(exc, httpx.TimeoutException):
        return "Request timed out"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text:
            return "Host name could not be resolved"
        if "certificate" in text or "ssl" in text:
            return "TLS certificate verification failed"
        if "refused" in text:
            return "Connection refused"
        return "Could not connect"
    return "Network error"


async def request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    """Perform a request, translating failures into ``IntegrationError``."""
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TransportError as exc:
        raise IntegrationError(ErrorKind.UNREACHABLE, _describe_transport_error(exc)) from exc
    if response.status_code in (401, 403):
        raise IntegrationError(
            ErrorKind.AUTH, f"Credentials rejected (HTTP {response.status_code})"
        )
    if 300 <= response.status_code < 400:
        raise IntegrationError(
            ErrorKind.PROTOCOL,
            f"Unexpected redirect (HTTP {response.status_code}) — check the base URL",
        )
    if response.status_code == 404:
        raise IntegrationError(ErrorKind.PROTOCOL, f"Not found: {path} — check the base URL")
    if response.status_code >= 400:
        raise IntegrationError(ErrorKind.PROTOCOL, f"HTTP {response.status_code} from {path}")
    return response


async def get_json(client: httpx.AsyncClient, path: str, **kwargs: Any) -> Any:
    response = await request(client, "GET", path, **kwargs)
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise IntegrationError(ErrorKind.PROTOCOL, f"Response from {path} is too large")
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntegrationError(
            ErrorKind.PROTOCOL, f"Expected JSON from {path} — is this the right service?"
        ) from exc
