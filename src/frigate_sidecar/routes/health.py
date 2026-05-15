"""Liveness/version endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from frigate_sidecar import __version__

router = APIRouter(tags=["meta"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}
