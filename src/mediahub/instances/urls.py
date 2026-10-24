"""Validation and normalization of user-supplied service URLs."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class InvalidURLError(ValueError):
    pass


def normalize_base_url(raw: str) -> str:
    """Return a canonical ``scheme://host[:port][/path]`` or raise ``InvalidURLError``.

    LAN and private addresses are allowed (that's the point of a self-hosted dashboard);
    the protections are elsewhere: only the admin configures URLs, redirects are never
    followed, and the browser can never make the backend fetch an arbitrary URL.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidURLError("URL is required")
    if "://" not in value:
        raise InvalidURLError("Include the scheme, e.g. http://192.168.1.10:8096")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("Not a valid URL") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Only http:// and https:// URLs are supported")
    if not parts.hostname:
        raise InvalidURLError("URL has no host")
    if parts.username or parts.password:
        raise InvalidURLError("Don't put credentials in the URL; use the API key field")
    if parts.query or parts.fragment:
        raise InvalidURLError("Remove the ?query or #fragment from the URL")
    if any(c.isspace() for c in value):
        raise InvalidURLError("URL contains whitespace")
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))
