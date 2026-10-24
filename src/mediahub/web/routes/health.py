"""Unauthenticated liveness endpoint used by the Docker HEALTHCHECK."""

from __future__ import annotations

from fastapi import APIRouter

from mediahub import __version__

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
