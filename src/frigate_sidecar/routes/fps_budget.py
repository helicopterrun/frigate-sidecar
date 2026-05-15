"""FPS-budget page: live snapshot of detector inference budget vs demand."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar.analysis import fps_budget
from frigate_sidecar.frigate_api import FrigateAPIError

router = APIRouter(tags=["fps-budget"])


def _util_css(util_pct: float) -> str:
    """Color band for the headline utilization number."""
    if util_pct > 85:
        return "noise"
    if util_pct > 65:
        return "warn"
    if util_pct < 20:
        return "muted"
    return "ok"


@router.get("/fps-budget", response_class=HTMLResponse)
def fps_budget_view(request: Request) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    result: dict[str, Any] | None = None
    error: str | None = None
    util_css = "muted"
    try:
        result = fps_budget.analyze(frigate_base_url=settings.frigate.base_url)
        util_css = _util_css(float(result["utilization_pct"]))
    except FrigateAPIError as exc:
        error = f"Frigate API unreachable: {exc}"

    return templates.TemplateResponse(
        request,
        "fps_budget.html",
        {
            "result": result,
            "error": error,
            "util_css": util_css,
            "counts": {},
        },
    )
