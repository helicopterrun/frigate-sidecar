"""Zone handling settings page: place classes, per-zone routing overrides,
and camera adjacency, edited in the browser against `/v1/push/settings`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["zones"])


@router.get("/zones", response_class=HTMLResponse)
def zones_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "zones.html", {})
