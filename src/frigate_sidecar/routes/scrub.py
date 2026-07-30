"""`/v1` scrub-cache + recording-coverage read layer.

Serves the capability probe and the coverage/reel endpoints straight from
`frigate.db` (read-only, already opened via `db.open_frigate_ro`) -- no
generation required for `/v1/coverage`, which alone removes the bug class
where "nothing recorded" and "not fetched" looked identical to the client.

Sprite-sheet generation (the actual scrub cache) is a separate, larger piece
(docs/scrub-cache-and-proxy-spec.md §5) not yet implemented here; this module
currently serves `/v1/capabilities` (reporting `scrub_cache.enabled=false`
until the generator lands) and `/v1/coverage/{camera}`.

See docs/scrub-cache-and-proxy-spec.md §4 for the full contract this answers to,
and §3.2 for why `/v1/*` requires the same Frigate auth cookie as `/api/*`.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from frigate_sidecar import __version__, db
from frigate_sidecar.frigate_api import FrigateAPIError, FrigateClient
from frigate_sidecar.scrub import grid
from frigate_sidecar.scrub.motion import aggregate_motion, safe_fetch_scale

router = APIRouter(prefix="/v1", tags=["v1"])

# Error vocabulary (docs spec §4.0) -- always paired with a machine-readable
# `error` field so the client can distinguish "nothing here yet" from "broken".
_ERR_CAMERA_UNKNOWN = "camera_unknown"
_ERR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
_ERR_NOT_GENERATED = "not_generated"
_ERR_BAD_RANGE = "bad_range"

_IMMUTABLE = "public, max-age=31536000, immutable"


def _etag_for(body: dict[str, Any]) -> str:
    digest = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()  # noqa: S324
    return f'"{digest}"'


def _etagged(request: Request, body: dict[str, Any]) -> Response:
    """§4.0: ETag on /v1/coverage and /v1/reel, 304 on a matching If-None-Match."""
    etag = _etag_for(body)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=body, headers={"ETag": etag})

# Per-session cookie-validation cache for /v1 auth (§3.2): validating against
# Frigate on every request would add real latency, so a cookie that validated
# once is trusted for this long. Keyed on the raw cookie header value.
_AUTH_CACHE_TTL_S = 60.0
_auth_cache: dict[str, float] = {}  # cookie value -> expiry (monotonic-ish, uses time.time())


async def _require_frigate_auth(request: Request) -> None:
    """Validate the client's Frigate session cookie, caching a pass for ~60s.

    Implements docs spec §3.2 finding 5 (option (a)): `/v1` must never be less
    protected than `/api`, which the proxy already requires a Frigate session
    for. Costs the client nothing -- Elsinore already sends its Frigate cookie
    on every request.
    """
    settings = request.app.state.settings
    cookie = request.headers.get("cookie", "")
    if not cookie:
        raise HTTPException(status_code=401, detail="frigate session required")

    now = time.time()
    expiry = _auth_cache.get(cookie)
    if expiry is not None and expiry > now:
        return

    upstream = f"{settings.frigate.proxy_base_url.rstrip('/')}/api/version"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(upstream, headers={"cookie": cookie})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"{_ERR_UPSTREAM_UNAVAILABLE}: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="frigate session invalid")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=_ERR_UPSTREAM_UNAVAILABLE)

    _auth_cache[cookie] = now + _AUTH_CACHE_TTL_S


def _known_cameras(conn: Any) -> set[str]:
    rows = conn.execute("SELECT DISTINCT camera FROM recordings").fetchall()
    return {row["camera"] for row in rows}


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    """No auth required -- this is the one `/v1` endpoint the client probes
    before it knows whether the sidecar is even reachable."""
    settings = request.app.state.settings
    generated = False
    generated_cameras: list[str] = list(settings.scrub.cameras)
    if settings.scrub.enabled:
        conn = db.open_sidecar(settings.sidecar.db_path)
        try:
            rows = conn.execute("SELECT DISTINCT camera FROM scrub_buckets").fetchall()
            cams_with_data = {r["camera"] for r in rows}
        finally:
            conn.close()
        if not generated_cameras:
            generated_cameras = sorted(cams_with_data)
        else:
            generated_cameras = [c for c in generated_cameras if c in cams_with_data] or list(
                settings.scrub.cameras
            )
        generated = bool(cams_with_data)

    return {
        "version": __version__,
        "scrub_cache": {
            "enabled": settings.scrub.enabled,
            "format": settings.scrub.format,
            "cameras": generated_cameras,
            "generated": generated,
        },
        "proxy": {"enabled": settings.proxy.enabled},
        "push": {"enabled": False},
    }


@router.get("/coverage/{camera}")
async def coverage(camera: str, start: float, end: float, request: Request) -> Any:
    """Recording coverage (§4.4) -- what Frigate actually recorded, read live
    from `frigate.db` so it never drifts from reality."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        result = db.recording_coverage(conn, camera, start, end, now=time.time())
    finally:
        conn.close()

    result["retention_days"] = settings.scrub.retention_days
    return _etagged(request, result)


def _bucket_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": row["start_ts"],
        "end": row["end_ts"],
        "interval": row["interval_s"],
        "width": row["width"],
        "height": row["height"],
    }


@router.get("/scrub/{camera}/coverage")
async def scrub_coverage(camera: str, start: float, end: float, request: Request) -> Any:
    """Scrub-cache coverage (§4.2) -- what sprite data exists, distinct from
    recording coverage (§4.4). Past `retention_days` there is nothing to
    sample and never will be; the client distinguishes that from "lagging" by
    comparing the queried range against `retention_days` (both already in the
    response) -- no separate flag needed, per spec.
    """
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    if end <= start:
        raise HTTPException(
            status_code=400, detail={"error": _ERR_BAD_RANGE, "message": "end must be > start"}
        )

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        bucket_rows = db.list_scrub_buckets(conn, camera, start, end)
        generated_through = db.latest_generated_through(conn, camera) or 0.0
    finally:
        conn.close()

    return {
        "camera": camera,
        "buckets": [_bucket_json(r) for r in bucket_rows],
        "generated_through": generated_through,
        "retention_days": settings.scrub.retention_days,
    }


@router.get("/scrub/{camera}/sheets")
async def scrub_sheets(camera: str, start: float, end: float, request: Request) -> Any:
    """Sheet index for a window (§4.3) -- content-addressed, immutable URLs
    keyed by (start, interval, count)."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    if end <= start:
        raise HTTPException(
            status_code=400, detail={"error": _ERR_BAD_RANGE, "message": "end must be > start"}
        )

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        sheet_rows = db.list_scrub_sheets(conn, camera, start, end)
    finally:
        conn.close()

    sheets = [
        {
            "url": grid.sheet_url(camera, r["start_ts"], r["interval_s"], r["count"]),
            "start": r["start_ts"],
            "interval": r["interval_s"],
            "cols": r["cols"],
            "rows": r["rows"],
            "cell_w": r["cell_w"],
            "cell_h": r["cell_h"],
            "count": r["count"],
        }
        for r in sheet_rows
    ]
    return {"sheets": sheets}


@router.get("/scrub/{camera}/sheet/{spec}")
async def scrub_sheet_image(camera: str, spec: str, request: Request) -> Any:
    """Serve one sheet image (§4.3). `spec` is `{start}-{interval}-{count}.jpg`
    -- every version of a still-filling sheet is its own immutable object, so
    the header is unconditional (no freshness reasoning exists anywhere in
    this path)."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    try:
        start, interval, count = grid.parse_sheet_spec(spec)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"error": _ERR_NOT_GENERATED, "message": str(exc)}
        ) from exc

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = db.get_scrub_sheet(conn, camera, start, interval, count)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_NOT_GENERATED, "message": "sheet not generated"},
        )

    path = Path(settings.scrub.cache_dir) / row["path"]
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_NOT_GENERATED, "message": "sheet file missing on disk"},
        )
    media_type = "image/webp" if path.suffix == ".webp" else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": _IMMUTABLE})


async def _fetch_and_aggregate_motion(
    settings: Any, camera: str, start: float, end: float, scale: float
) -> list[float]:
    fetch_scale = safe_fetch_scale(scale)
    try:
        with FrigateClient(settings.frigate.base_url) as fc:
            raw = fc.activity_motion(camera, start, end, fetch_scale)
    except FrigateAPIError:
        raw = []

    points: list[tuple[float, float]] = []
    for item in raw:
        ts = item.get("start_time")
        val = item.get("motion")
        if ts is None or val is None:
            continue
        points.append((float(ts), float(val)))
    return aggregate_motion(points, start, end, scale)


@router.get("/motion/{camera}")
async def motion(camera: str, start: float, end: float, scale: float, request: Request) -> Any:
    """Total motion (§4.6) -- any `scale`, always covering the full requested
    `[start, end)`, zero-filled where there is genuinely no data. Fixes
    Frigate's two measured cliffs (all-zero wide scale, short-window
    truncation) by fetching at a safe scale and aggregating ourselves."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    if end <= start:
        raise HTTPException(
            status_code=400, detail={"error": _ERR_BAD_RANGE, "message": "end must be > start"}
        )
    values = await _fetch_and_aggregate_motion(settings, camera, start, end, scale)
    return {"start": start, "interval": scale, "values": values}


def _events_json(conn: Any, camera: str, start: float, end: float) -> list[dict[str, Any]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
    rows = conn.execute(
        "SELECT * FROM event WHERE camera = ? AND start_time < ? "
        "AND (end_time IS NULL OR end_time > ?) ORDER BY start_time",
        (camera, end, start),
    ).fetchall()
    out = []
    for row in rows:
        zones_raw = row["zones"] if "zones" in cols else None
        try:
            zones = json.loads(zones_raw) if zones_raw else []
        except (json.JSONDecodeError, TypeError):
            zones = []
        score = row["score"] if "score" in cols else None
        out.append(
            {
                "id": row["id"],
                "label": row["label"],
                "zones": zones,
                "start": row["start_time"],
                # events[].end is nullable and null means "still in progress"
                # (§4.5) -- must not synthesize a placeholder timestamp.
                "end": row["end_time"],
                "score": score,
            }
        )
    return out


@router.get("/reel/{camera}")
async def reel(
    camera: str, start: float, end: float, request: Request, motion_scale: float = 10.0
) -> Any:
    """One call per reel window (§4.5) -- collapses coverage + scrub buckets +
    motion + events into one response with one cache lifetime."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    if end <= start:
        raise HTTPException(
            status_code=400, detail={"error": _ERR_BAD_RANGE, "message": "end must be > start"}
        )

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        coverage_result = db.recording_coverage(conn, camera, start, end, now=time.time())
        events = _events_json(conn, camera, start, end)
    finally:
        conn.close()

    sidecar_conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        bucket_rows = db.list_scrub_buckets(sidecar_conn, camera, start, end)
    finally:
        sidecar_conn.close()

    frames = [
        {
            "start": r["start_ts"],
            "interval": r["interval_s"],
            "count": round((min(r["end_ts"], end) - r["start_ts"]) / r["interval_s"]),
        }
        for r in bucket_rows
    ]
    motion_values = await _fetch_and_aggregate_motion(settings, camera, start, end, motion_scale)

    body = {
        "queried": [start, end],
        "recorded": coverage_result["recorded"],
        "latest_segment_end": coverage_result["latest_segment_end"],
        "authoritative_through": coverage_result["authoritative_through"],
        "frames": frames,
        "motion": {"start": start, "interval": motion_scale, "values": motion_values},
        "events": events,
    }
    return _etagged(request, body)


@router.get("/highlights/{camera}")
async def highlights(camera: str, request: Request, before: float, limit: int = 10) -> Any:
    """Ranked index of interesting moments (§4.7), precomputed from `event`
    rows -- `reason` is a Frigate object label (person/car/package/...), the
    same vocabulary the client already maps to lanes."""
    await _require_frigate_auth(request)
    settings = request.app.state.settings
    limit = max(1, min(limit, 100))

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
        score_col = "top_score" if "top_score" in cols else "score"
        rows = conn.execute(
            f"SELECT id, label, start_time, end_time, {score_col} AS score FROM event "
            "WHERE camera = ? AND start_time < ? ORDER BY start_time DESC LIMIT ?",
            (camera, before, limit),
        ).fetchall()
    finally:
        conn.close()

    return {
        "highlights": [
            {
                "start": r["start_time"],
                "end": r["end_time"],
                "reason": r["label"],
                "score": r["score"],
            }
            for r in rows
        ]
    }
