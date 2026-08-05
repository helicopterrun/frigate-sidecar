"""Zone-hits page: per-camera zone hit counts + mask candidates.

Same compute as `fsc analysis zone-hits` / `GET /analysis/zone-hits`, surfaced
as a page (mirrors routes/fps_budget.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar.analysis import zone_hits

router = APIRouter(tags=["zone-hits"])


@router.get("/zone-hits", response_class=HTMLResponse)
def zone_hits_view(
    request: Request, days: int = 30, camera: str | None = None
) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = zone_hits.analyze(
            frigate_db=settings.frigate.db_path,
            sidecar_db=settings.sidecar.db_path,
            days=days,
            camera=camera,
        )
    except Exception as exc:  # noqa: BLE001 -- surface, don't 500, like /motion
        error = str(exc)

    cameras = sorted({h["camera"] for h in result["hits"]}) if result else []
    return templates.TemplateResponse(
        request,
        "zone_hits.html",
        {
            "result": result,
            "error": error,
            "days": days,
            "camera": camera or "",
            "cameras": cameras,
            "counts": {},
        },
    )
