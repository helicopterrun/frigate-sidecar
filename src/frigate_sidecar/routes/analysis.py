"""HTTP endpoints for the read-only analysis modules.

Each endpoint mirrors the corresponding `fsc analysis ...` CLI subcommand
and returns the same structured payload as JSON.
"""

from __future__ import annotations

from typing import Any

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
    return request.app.state.settings


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
