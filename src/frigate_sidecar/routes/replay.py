"""Push replay workbench: fire canned scenarios from a phone browser."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from frigate_sidecar.push import replay

router = APIRouter(tags=["replay"])


class RunRequest(BaseModel):
    scenarios: list[str] = Field(..., min_length=1)
    speed: float = Field(1.0, gt=0)
    dry_run: bool = False
    stagger: float = Field(8.0, ge=0)


@router.get("/replay", response_class=HTMLResponse)
def replay_view(request: Request) -> Any:
    templates = request.app.state.templates
    scenarios = replay.list_scenarios()
    push_enabled = request.app.state.settings.push.enabled
    return templates.TemplateResponse(
        request,
        "replay.html",
        {"scenarios": scenarios, "push_enabled": push_enabled, "counts": {}},
    )


@router.get("/replay/scenarios")
def replay_scenarios() -> JSONResponse:
    return JSONResponse({"scenarios": replay.list_scenarios()})


@router.post("/replay/run")
async def replay_run(body: RunRequest, request: Request) -> JSONResponse:
    for name in body.scenarios:
        try:
            replay.resolve_scenario_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail=f"unknown scenario: {name}")

    if not body.dry_run and not request.app.state.settings.push.enabled:
        raise HTTPException(status_code=503, detail="push is not enabled (live run requires MQTT)")

    push_settings = request.app.state.settings.push if not body.dry_run else None

    try:
        run = await replay.start_run(
            body.scenarios,
            speed=body.speed,
            dry_run=body.dry_run,
            push_settings=push_settings,
            stagger=body.stagger,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return JSONResponse(run.to_dict())


@router.get("/replay/status")
def replay_status() -> JSONResponse:
    run = replay.get_current_run()
    if run is None:
        return JSONResponse({"run": None})
    return JSONResponse({"run": run.to_dict()})
