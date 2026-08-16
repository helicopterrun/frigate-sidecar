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


@router.get("/replay/capture-window")
def replay_capture_window(
    request: Request, minutes: float = 15.0, camera: str | None = None,
) -> JSONResponse:
    """Recent tracks from the MQTT flight recorder, shaped for map trails:
    per (camera, track_id) -> label + (x, y, t) path points, newest tracks
    first, capped so a busy ring can't flood the browser."""
    import time as _time
    from pathlib import Path

    from frigate_sidecar.push import capture

    settings = request.app.state.settings
    capture_path = settings.push.capture_path or str(
        Path(settings.push.push_settings_path).parent / "mqtt-capture.jsonl"
    )
    paths = [Path(capture_path + ".1"), Path(capture_path)]
    start_ts = _time.time() - max(1.0, min(minutes, 24 * 60)) * 60.0
    rows = capture.read_window(paths, start_ts=start_ts, camera=camera)

    tracks: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not str(row.get("topic", "")).endswith("events"):
            continue
        after = (row.get("payload") or {}).get("after") or {}
        cam, tid = after.get("camera"), after.get("id")
        if not cam or not tid:
            continue
        entry = tracks.setdefault(
            (cam, tid),
            {"camera": cam, "track_id": tid, "label": after.get("label") or "", "points": {}},
        )
        for p in after.get("path_data") or []:
            try:
                if len(p) == 2 and isinstance(p[0], (list, tuple)):
                    (x, y), t = p
                else:
                    x, y, t = p[0], p[1], p[2]
                entry["points"][round(float(t), 3)] = [
                    round(float(x), 4), round(float(y), 4), round(float(t), 3),
                ]
            except (TypeError, ValueError, IndexError):
                continue

    out = []
    for entry in tracks.values():
        points = [entry["points"][t] for t in sorted(entry["points"])]
        if len(points) < 2:
            continue
        out.append({
            "camera": entry["camera"], "track_id": entry["track_id"],
            "label": entry["label"], "points": points[:400],
        })
    out.sort(key=lambda e: e["points"][-1][2], reverse=True)
    return JSONResponse({"tracks": out[:500]})


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
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — the UI needs JSON, not an HTML 500
        # A live run touches MQTT; a broker connect failure (refused, timeout,
        # bad credentials) used to escape as an unhandled 500 whose HTML error
        # page broke the replay page's JSON parse ("Unexpected token '<'",
        # observed 2026-08-14). Surface the real cause as JSON instead.
        raise HTTPException(status_code=502, detail=f"replay failed: {exc}")

    return JSONResponse(run.to_dict())


@router.get("/replay/status")
def replay_status() -> JSONResponse:
    run = replay.get_current_run()
    if run is None:
        return JSONResponse({"run": None})
    return JSONResponse({"run": run.to_dict()})
