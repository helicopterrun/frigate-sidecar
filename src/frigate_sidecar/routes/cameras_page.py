"""Camera calibration page: draw a per-camera "toward home" vector on the
live snapshot (drives the LA heading chip) and arrange cameras on a small
layout map (drives neighbor suggestions)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["cameras"])


@router.get("/cameras", response_class=HTMLResponse)
def cameras_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "cameras.html", {})
