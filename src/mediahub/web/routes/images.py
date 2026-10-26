"""Image endpoints.

Real posters are proxied through signed references (``integrations/image_refs.py``) so the
browser never sees an API key and can't make MediaHub fetch arbitrary URLs. Fetched images are
kept in an on-disk LRU cache. Demo mode serves generated SVG posters.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from mediahub.context import AppContext
from mediahub.integrations import image_refs
from mediahub.integrations.errors import IntegrationError
from mediahub.web.deps import Ctx, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

FETCH_TIMEOUT_S = 10.0
# Raster only: an SVG from upstream could carry script if opened directly.
ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
IMAGE_HEADERS = {
    # The URL contains the upstream image tag, so a given URL's bytes never change.
    "Cache-Control": "private, max-age=604800, immutable",
    "Content-Security-Policy": "default-src 'none'",
}

_PALETTES = [
    ("#1e3a8a", "#7c3aed"),
    ("#7f1d1d", "#ea580c"),
    ("#064e3b", "#0891b2"),
    ("#312e81", "#db2777"),
    ("#1f2937", "#2563eb"),
    ("#422006", "#ca8a04"),
    ("#134e4a", "#65a30d"),
    ("#3b0764", "#2563eb"),
]


def _wrap(title: str, width: int = 12, lines: int = 4) -> list[str]:
    words, out, line = title.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            out.append(line)
            line = word
        else:
            line = candidate
    if line:
        out.append(line)
    return out[:lines]


@router.get("/img/demo/{slug}.svg")
async def demo_poster(slug: str, t: Annotated[str, Query(max_length=60)] = "") -> Response:
    digest = hashlib.sha1((slug + t).encode(), usedforsecurity=False).digest()
    top, bottom = _PALETTES[digest[0] % len(_PALETTES)]
    lines = _wrap(t or "MediaHub")
    text = "".join(
        f'<text x="20" y="{200 - (len(lines) - i) * 22}" font-size="19" font-weight="700" '
        f'fill="#fff" font-family="system-ui,sans-serif">{escape(line)}</text>'
        for i, line in enumerate(lines)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 240">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" '
        f'stop-color="{bottom}"/><stop offset="1" stop-color="{top}"/></linearGradient></defs>'
        '<rect width="160" height="240" fill="url(#g)"/>'
        '<circle cx="128" cy="40" r="60" fill="#fff" opacity="0.08"/>'
        f"{text}</svg>"
    )
    return Response(
        svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@router.get("/img/{instance_id}/{item_id}/{tag}/{signature}")
async def proxied_image(
    ctx: Ctx, instance_id: str, item_id: str, tag: str, signature: str
) -> Response:
    return await serve_signed_image(ctx, instance_id, item_id, tag, signature)


@router.get("/img/peer/{peer_id}/{instance_id}/{item_id}/{tag}/{signature}")
async def peer_image(
    ctx: Ctx, peer_id: str, instance_id: str, item_id: str, tag: str, signature: str
) -> Response:
    """A poster on a linked peer, fetched through its ``/peer/v1/img`` (it checks the
    signature, which only it can make)."""
    parts = (peer_id, instance_id, item_id, tag, signature)
    if not all(image_refs.valid_part(p) for p in parts):
        raise HTTPException(404, "Image not found")
    key = "peer/" + "/".join(parts[:4])
    cached = await run_in_threadpool(ctx.images.get, key)
    if cached is not None:
        return Response(cached[0], media_type=cached[1], headers=IMAGE_HEADERS)
    client = ctx.monitor.peer_client(peer_id)
    if client is None:
        raise HTTPException(404, "Image not found")
    try:
        fetched = await asyncio.wait_for(client.fetch_image("/".join(parts[1:])), FETCH_TIMEOUT_S)
    except TimeoutError:
        fetched = None
    return await _store_and_send(ctx, key, fetched)


async def serve_signed_image(
    ctx: AppContext, instance_id: str, item_id: str, tag: str, signature: str
) -> Response:
    """An image on a local instance, by a reference this server signed."""
    if not image_refs.verify(instance_id, item_id, tag, signature):
        raise HTTPException(404, "Image not found")
    key = f"{instance_id}/{item_id}/{tag}"
    cached = await run_in_threadpool(ctx.images.get, key)
    if cached is not None:
        return Response(cached[0], media_type=cached[1], headers=IMAGE_HEADERS)
    integration = ctx.monitor.integration(instance_id)
    if integration is None:
        raise HTTPException(404, "Image not found")
    try:
        fetched = await asyncio.wait_for(
            integration.fetch_image(f"{item_id}/{tag}"), FETCH_TIMEOUT_S
        )
    except (IntegrationError, TimeoutError):
        fetched = None
    return await _store_and_send(ctx, key, fetched)


async def _store_and_send(ctx: AppContext, key: str, fetched: tuple[bytes, str] | None) -> Response:
    if fetched is None or fetched[1] not in ALLOWED_TYPES:
        raise HTTPException(404, "Image not available")
    data, content_type = fetched
    await run_in_threadpool(ctx.images.put, key, data, content_type)
    return Response(data, media_type=content_type, headers=IMAGE_HEADERS)
