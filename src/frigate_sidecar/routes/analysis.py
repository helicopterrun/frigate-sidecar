"""HTTP endpoints for the read-only analysis modules.

Each endpoint mirrors the corresponding `fsc analysis ...` CLI subcommand
and returns the same structured payload as JSON.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from frigate_sidecar.analysis import (
    annotation_offset,
    fps_budget,
    motion_active,
    motion_compare,
    motion_rate,
    pull_events,
    score_histogram,
    zone_hits,
)
from frigate_sidecar.config import Settings
from frigate_sidecar.frigate_api import FrigateAPIError

router = APIRouter(prefix="/analysis", tags=["analysis"])


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


@router.get("/score-histogram")
def score_histogram_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
    camera: str | None = None,
    label: str | None = None,
    min_samples: int = Query(30, ge=1),
) -> dict[str, Any]:
    s = _settings(request)
    return score_histogram.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days, camera=camera, label=label, min_samples=min_samples,
    )


@router.get("/motion-rate")
def motion_rate_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
) -> list[dict[str, Any]]:
    s = _settings(request)
    return motion_rate.analyze(frigate_db=s.frigate.db_path, days=days)


@router.get("/fps-budget")
def fps_budget_endpoint(request: Request) -> dict[str, Any]:
    s = _settings(request)
    try:
        return fps_budget.analyze(frigate_base_url=s.frigate.base_url)
    except FrigateAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/motion-active")
def motion_active_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
) -> dict[str, Any]:
    s = _settings(request)
    try:
        return motion_active.analyze(frigate_base_url=s.frigate.base_url, days=days)
    except FrigateAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/motion-compare")
def motion_compare_endpoint(
    request: Request,
    baseline: str,
    target: str,
) -> dict[str, Any]:
    s = _settings(request)
    try:
        return motion_compare.analyze(
            frigate_base_url=s.frigate.base_url, baseline=baseline, target=target,
        )
    except FrigateAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        # Date-parse errors from parse_range bubble up here.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/zone-hits")
def zone_hits_endpoint(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    camera: str | None = None,
) -> dict[str, Any]:
    s = _settings(request)
    return zone_hits.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days, camera=camera,
    )


@router.get("/pull-events")
def pull_events_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
    camera: str | None = None,
    label: str | None = None,
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    """Returns up to `limit` events as a JSON array (capped for HTTP use)."""
    s = _settings(request)
    out: list[dict[str, Any]] = []
    for ev in pull_events.pull(
        frigate_db=s.frigate.db_path, days=days, camera=camera, label=label,
    ):
        out.append(ev)
        if len(out) >= limit:
            break
    return out


@router.get("/annotation-offset")
def annotation_offset_endpoint(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    camera: str | None = None,
) -> list[dict[str, Any]]:
    s = _settings(request)
    try:
        return annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days, camera=camera,
        )
    except annotation_offset.AnnotationOffsetUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


# --- Event-clock alignment (Settings page workflow) -------------------------
#
# The measurement sweeps a wider window than the CLI default: the skew this
# exists to find is "a few seconds", and a ±3 s sweep cannot see a 5 s offset.
_MEASURE_WINDOW_MS = 8000


def _alignment_job(request: Request) -> dict[str, Any]:
    state = request.app.state
    if not hasattr(state, "alignment_job"):
        state.alignment_job = {
            "running": False, "results": None, "error": None, "measured_at": None
        }
    return cast(dict[str, Any], state.alignment_job)


@router.post("/annotation-offset/measure")
async def alignment_measure(request: Request, days: int = Query(3, ge=1, le=30)) -> Any:
    """Starts a background measurement pass; poll `state` for the result.

    Template-matching every candidate event thumbnail against recording
    snapshots takes minutes -- far past an HTTP timeout -- so this returns
    immediately and the Settings page polls."""
    import asyncio

    job = _alignment_job(request)
    if job["running"]:
        raise HTTPException(status_code=409, detail="a measurement is already running")
    s = _settings(request)
    job.update(running=True, error=None)

    def _run() -> list[dict[str, Any]]:
        return annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days,
            search_window_ms=_MEASURE_WINDOW_MS,
        )

    async def _task() -> None:
        import time as _time

        try:
            job["results"] = await asyncio.to_thread(_run)
            job["measured_at"] = _time.time()
        except Exception as exc:  # noqa: BLE001 -- surfaced to the page, not raised
            job["error"] = str(exc)
        finally:
            job["running"] = False

    request.app.state.alignment_task = asyncio.create_task(_task())
    return {"started": True}


@router.get("/annotation-offset/events")
def alignment_events(
    request: Request,
    camera: str,
    limit: int = Query(12, ge=1, le=25),
) -> list[dict[str, Any]]:
    """Recent finished events for the manual-calibration picker.

    Looser than the automated measurement's filter: any labeled event with a
    snapshot will do, because a human judges the match. Events younger than a
    minute are skipped -- the recording segment covering them may not be
    committed yet, so the filmstrip would be all 404s.
    """
    import time

    from frigate_sidecar import db

    s = _settings(request)
    conn = db.open_frigate_ro(s.frigate.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, label, sub_label, start_time, end_time, top_score
              FROM event
             WHERE camera = ? AND has_snapshot = 1
               AND end_time IS NOT NULL AND start_time < ?
             ORDER BY start_time DESC
             LIMIT ?
            """,
            (camera, time.time() - 60.0, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "sub_label": r["sub_label"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "top_score": r["top_score"],
        }
        for r in rows
    ]


@router.get("/annotation-offset/frame/{camera}")
def alignment_frame(request: Request, camera: str, ts: float = Query(...)) -> Any:
    """A recording frame at wall-clock `ts`, for the calibration filmstrip.

    Recordings are immutable, so successful frames cache hard -- nudging back
    and forth over the same offsets stays in the browser cache."""
    import math
    import time

    from fastapi.responses import Response

    from frigate_sidecar.frigate_api import FrigateAPIError, FrigateClient

    now = time.time()
    if not math.isfinite(ts) or ts <= 0 or ts > now or now - ts > 30 * 86400:
        raise HTTPException(status_code=422, detail="ts out of range")
    s = _settings(request)
    try:
        with FrigateClient(s.frigate.base_url) as fc:
            jpeg, status = fc.recording_snapshot(camera, ts)
    except FrigateAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if jpeg is None:
        raise HTTPException(status_code=404, detail="no recording covers ts")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/annotation-offset/state")
def alignment_state(request: Request) -> Any:
    """The measurement job plus what's currently in effect per camera."""
    from frigate_sidecar import db
    from frigate_sidecar.faces.crosscam import annotation_offset_ms

    s = _settings(request)
    job = _alignment_job(request)
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        applied = db.event_clock_offsets(conn)
    finally:
        conn.close()
    cameras = sorted(
        {r["camera"] for r in (job["results"] or [])} | set(applied)
    )
    # Every camera known to Frigate, so the manual calibrator can reach ones
    # with no measurement and no applied offset.
    all_cameras: list[str] = []
    try:
        fconn = db.open_frigate_ro(s.frigate.db_path)
        try:
            all_cameras = sorted(
                r["camera"] for r in fconn.execute("SELECT DISTINCT camera FROM event")
            )
        finally:
            fconn.close()
    except db.FrigateDBMissingError:
        all_cameras = cameras
    config_ms = {
        cam: annotation_offset_ms(str(s.frigate.config_path), cam)
        for cam in sorted(set(cameras) | set(all_cameras))
    }
    return {
        "running": job["running"],
        "error": job["error"],
        "measured_at": job["measured_at"],
        "results": job["results"],
        "applied_ms": applied,
        "config_ms": config_ms,
        "cameras": all_cameras,
    }


@router.post("/annotation-offset/apply")
async def alignment_apply(request: Request) -> Any:
    """Writes sidecar-side offsets: body `{"offsets": {camera: ms, ...}}`.

    An offset of 0 clears the override. Frigate's own
    `detect.annotation_offset` still wins over these when set."""
    from frigate_sidecar import db
    from frigate_sidecar.routes import scrub as scrub_routes

    body = await request.json()
    offsets = body.get("offsets")
    if not isinstance(offsets, dict) or not offsets:
        raise HTTPException(status_code=422, detail="offsets must be a non-empty object")
    clean: dict[str, int] = {}
    for cam, ms in offsets.items():
        try:
            value = int(ms)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"bad offset for {cam}") from exc
        if abs(value) > 60_000:
            raise HTTPException(status_code=422, detail=f"offset for {cam} out of range")
        clean[str(cam)] = value

    s = _settings(request)
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        for cam, ms in clean.items():
            db.set_event_clock_offset(conn, cam, ms)
    finally:
        conn.close()
    scrub_routes.invalidate_event_clock_offsets()
    return {"applied": clean}
