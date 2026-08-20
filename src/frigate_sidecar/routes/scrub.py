"""`/v1` scrub-cache + recording-coverage read layer.

Serves the capability probe, the coverage/reel endpoints and the sprite-sheet
index/images. Coverage comes straight from `frigate.db` (read-only, via
`db.open_frigate_ro`) -- no generation required for `/v1/coverage`, which alone
removes the bug class where "nothing recorded" and "not fetched" looked
identical to the client. The sheets themselves are produced by the generator
(`scrub/generator.py`, docs spec §5) and read back here.

See docs/scrub-cache-and-proxy-spec.md §4 for the full contract this answers to.
Auth (§3.2 -- `/v1` is never less protected than `/api`) is applied centrally in
`frigate_sidecar.auth`, which covers every sidecar-owned route, not just these.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from frigate_sidecar import __version__, db, zones
from frigate_sidecar.frigate_api import (
    FrigateAPIError,
    async_activity_motion,
    get_async_client,
)
from frigate_sidecar.frigate_config import recording_retention_days
from frigate_sidecar.push import policy_settings
from frigate_sidecar.scrub import grid
from frigate_sidecar.scrub.motion import aggregate_motion, safe_fetch_scale

router = APIRouter(prefix="/v1", tags=["v1"])

# Error vocabulary (docs spec §4.0) -- always paired with a machine-readable
# `error` field so the client can distinguish "nothing here yet" from "broken".
_ERR_CAMERA_UNKNOWN = "camera_unknown"
_ERR_NOT_GENERATED = "not_generated"
_ERR_BAD_RANGE = "bad_range"
# `upstream_unavailable` also belongs to this vocabulary; it is raised by the
# shared auth gate (frigate_sidecar.auth.ERR_UPSTREAM_UNAVAILABLE).

_IMMUTABLE = "public, max-age=31536000, immutable"

# Upper bound on the series length a single request may ask us to build.
# `values` is materialised in memory, so an unbounded (end-start)/scale was an
# allocate-until-OOM lever: scale=0.001 over a multi-day window asks for
# hundreds of millions of buckets.
MAX_MOTION_POINTS = 20_000


def _etag_for(body: dict[str, Any]) -> str:
    digest = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()  # noqa: S324
    return f'"{digest}"'


def _etagged(request: Request, body: dict[str, Any]) -> Response:
    """§4.0: ETag on /v1/coverage and /v1/reel, 304 on a matching If-None-Match."""
    etag = _etag_for(body)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=body, headers={"ETag": etag})


#: How long a `/v1/scrub/*` request may reuse the known-camera set cached on
#: `app.state` instead of re-querying `frigate.db`. The set changes only when
#: a camera is added/removed from the fleet, so a per-request
#: `SELECT DISTINCT camera FROM recordings` on every scrub-coverage/sheets/
#: reel/highlights call was paying a query nothing in that window needed.
_CAMERA_CACHE_TTL_S = 30.0


def _known_cameras(app_state: Any, conn: Any) -> set[str]:
    cached = getattr(app_state, "scrub_known_cameras_cache", None)
    now = time.time()
    if cached is not None and now - cached[0] < _CAMERA_CACHE_TTL_S:
        return cached[1]
    rows = conn.execute("SELECT DISTINCT camera FROM recordings").fetchall()
    cameras = {row["camera"] for row in rows}
    app_state.scrub_known_cameras_cache = (now, cameras)
    return cameras


def _bad_range(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": _ERR_BAD_RANGE, "message": message})


def _require_window(start: float, end: float) -> None:
    if not (end > start):
        raise _bad_range("end must be > start")


def _require_series(start: float, end: float, scale: float) -> None:
    """Reject a window/scale pair whose series we refuse to materialise."""
    _require_window(start, end)
    if scale <= 0:
        raise _bad_range("scale must be > 0")
    if (end - start) / scale > MAX_MOTION_POINTS:
        raise _bad_range(
            f"requested range needs more than {MAX_MOTION_POINTS} points at scale={scale}; "
            "widen scale or narrow the window"
        )


def _require_known_camera(request: Request, camera: str) -> None:
    settings = request.app.state.settings
    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        known = _known_cameras(request.app.state, conn)
    finally:
        conn.close()
    if camera not in known:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
        )


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    """No auth required -- this is the one `/v1` endpoint the client probes
    before it knows whether the sidecar is even reachable."""
    settings = request.app.state.settings
    generated = False
    generated_cameras: list[str] = list(settings.scrub.cameras)
    if settings.scrub.enabled:
        cams_with_data = await db.with_sidecar(
            settings.sidecar.db_path,
            lambda conn: {
                r["camera"]
                for r in conn.execute("SELECT DISTINCT camera FROM scrub_buckets").fetchall()
            },
        )
        # Cached buckets can outlive a camera rename; don't advertise ghosts.
        configured = zones.configured_camera_names(settings.frigate.config_path)
        if configured is not None:
            cams_with_data &= configured
        if not generated_cameras:
            generated_cameras = sorted(cams_with_data)
        else:
            generated_cameras = [c for c in generated_cameras if c in cams_with_data] or list(
                settings.scrub.cameras
            )
        generated = bool(cams_with_data)

    # What `/sheets?interval=` can be asked for: the two decode tiers plus
    # every configured derived tier. Configured, not queried-from-the-DB --
    # `cameras`/`generated` above already answer "has it backfilled"; this
    # just tells the client which cadences exist to select.
    intervals = sorted(
        {settings.scrub.recent_interval_s, settings.scrub.aged_interval_s}
        | set(settings.scrub.derived_intervals_s)
    )

    return {
        "version": __version__,
        "scrub_cache": {
            "enabled": settings.scrub.enabled,
            "format": settings.scrub.format,
            "cameras": generated_cameras,
            "generated": generated,
            "intervals": intervals,
        },
        "proxy": {"enabled": settings.proxy.enabled},
        "push": {
            "enabled": settings.push.enabled,
            "transport": settings.push.transport,
            # One alerts stack: which subjects the outcomes/routing tables
            # accept. The app hides the V3 ladder rows -- and omits their
            # keys from PUTs -- unless this list says they're routable.
            "attention_subjects": list(policy_settings.SUBJECTS_V3),
        },
        "decisions": {"enabled": True},
    }


@router.get("/coverage/{camera}")
async def coverage(camera: str, start: float, end: float, request: Request) -> Any:
    """Recording coverage (§4.4) -- what Frigate actually recorded, read live
    from `frigate.db` so it never drifts from reality."""
    settings = request.app.state.settings
    _require_window(start, end)

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(request.app.state, conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        result = db.recording_coverage(conn, camera, start, end, now=time.time())
    finally:
        conn.close()

    # Named for what it is. `retention_days` here was the *scrub cache's*
    # horizon on an endpoint otherwise entirely about what Frigate recorded, and
    # §4.2 tells clients to compare a queried range against `retention_days` to
    # tell "will never exist" from "still lagging" -- apply that to this
    # response and you conclude recordings stop at 4 days when the motion band
    # runs to 8. (That reasoning belongs to /v1/scrub/{camera}/coverage, which
    # reports its own `retention_days` and is unambiguous there.)
    result["scrub_retention_days"] = settings.scrub.retention_days
    result["recording_retention_days"] = recording_retention_days(
        settings.frigate.config_path, camera
    )
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
    settings = request.app.state.settings
    _require_window(start, end)
    _require_known_camera(request, camera)

    # Derived tiers (settings.scrub.derived_intervals_s) deliberately overlap
    # the recent/aged decode tiers in time, and each other. `list_scrub_buckets`
    # and the unfiltered `generated_through` both scan every tier, so leaving
    # derived tiers in would double- (or triple-)report coverage for any span
    # they also cover -- multiple bucket rows for one instant, and a
    # `generated_through` pulled ahead by whichever tier happens to be further
    # along. `grid.exclude_derived_buckets` drops them, keeping this response's
    # one-bucket-per-instant contract exactly (shared with `/v1/reel`, which
    # applies the same exclusion to its own `frames` list).
    def _read_coverage(conn: Any) -> tuple[list[Any], float]:
        bucket_rows = db.list_scrub_buckets(conn, camera, start, end)
        exclude = sorted(
            grid.excluded_derived_intervals(bucket_rows, settings.scrub.derived_intervals_s)
        )
        if exclude:
            bucket_rows = [r for r in bucket_rows if r["interval_s"] not in exclude]
            generated_through = (
                db.latest_generated_through(conn, camera, exclude_intervals_s=exclude) or 0.0
            )
        else:
            generated_through = db.latest_generated_through(conn, camera) or 0.0
        return bucket_rows, generated_through

    bucket_rows, generated_through = await db.with_sidecar(
        settings.sidecar.db_path, _read_coverage
    )

    return {
        "camera": camera,
        "buckets": [_bucket_json(r) for r in bucket_rows],
        "generated_through": generated_through,
        "retention_days": settings.scrub.retention_days,
    }


@router.get("/scrub/{camera}/sheets")
async def scrub_sheets(
    camera: str, start: float, end: float, request: Request, interval: float | None = None
) -> Any:
    """Sheet index for a window (§4.3) -- content-addressed, immutable URLs
    keyed by (start, interval, count).

    `interval` is optional and restricts the response to that one tier (e.g.
    a derived tier, which overlaps recent/aged in time) -- omit it to get
    every tier's sheets for the window, same as before this param existed.
    """
    settings = request.app.state.settings
    _require_window(start, end)
    _require_known_camera(request, camera)

    sheet_rows = await db.with_sidecar(
        settings.sidecar.db_path,
        lambda conn: db.list_scrub_sheets(conn, camera, start, end, interval=interval),
    )

    sheets = [
        {
            # Extension comes from the row's own on-disk path so the advertised
            # URL matches the bytes actually stored (jpeg vs webp).
            "url": grid.sheet_url(
                camera,
                r["start_ts"],
                r["interval_s"],
                r["count"],
                ext=Path(r["path"]).suffix,
            ),
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
    settings = request.app.state.settings
    _require_known_camera(request, camera)
    try:
        start, interval, count = grid.parse_sheet_spec(spec)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"error": _ERR_NOT_GENERATED, "message": str(exc)}
        ) from exc

    row = await db.with_sidecar(
        settings.sidecar.db_path,
        lambda conn: db.get_scrub_sheet(conn, camera, start, interval, count),
    )
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
    request: Request, camera: str, start: float, end: float, scale: float
) -> list[float]:
    settings = request.app.state.settings
    fetch_scale = safe_fetch_scale(scale)
    try:
        raw = await async_activity_motion(
            get_async_client(request.app),
            settings.frigate.base_url,
            camera,
            start,
            end,
            fetch_scale,
        )
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
    _require_series(start, end, scale)
    _require_known_camera(request, camera)
    values = await _fetch_and_aggregate_motion(request, camera, start, end, scale)
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
        score = db.event_top_score(row)
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
    settings = request.app.state.settings
    _require_series(start, end, motion_scale)

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(request.app.state, conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        coverage_result = db.recording_coverage(conn, camera, start, end, now=time.time())
        events = _events_json(conn, camera, start, end)
    finally:
        conn.close()

    bucket_rows = await db.with_sidecar(
        settings.sidecar.db_path,
        lambda conn: db.list_scrub_buckets(conn, camera, start, end),
    )

    # Same exclusion `/v1/scrub/{camera}/coverage` applies -- without it, a
    # window covered by both a decode tier and an overlapping derived tier
    # double-reports `frames` for the same span.
    bucket_rows = grid.exclude_derived_buckets(bucket_rows, settings.scrub.derived_intervals_s)

    frames = [
        {
            "start": r["start_ts"],
            "interval": r["interval_s"],
            "count": round((min(r["end_ts"], end) - r["start_ts"]) / r["interval_s"]),
        }
        for r in bucket_rows
    ]
    motion_values = await _fetch_and_aggregate_motion(request, camera, start, end, motion_scale)

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


def _cluster_highlights(
    highlights: list[dict[str, Any]], cluster_s: float
) -> list[dict[str, Any]]:
    """Collapse runs of highlights into one destination each.

    One person walking past a camera emits three or four events -- measured
    across three cameras, 40-50% of consecutive highlights are under 45s apart
    -- so a naive "jump to the next highlight" presses the same person four
    times. The gap is measured end-to-start, so a long event followed closely
    by another counts as continuing rather than as a new destination.

    `highlights` must be in ascending start order; the result keeps that order.
    """
    if cluster_s <= 0 or not highlights:
        return highlights

    def _extent(item: dict[str, Any]) -> float:
        """Where an item finishes, treating in-progress as still at its start."""
        return item["start"] if item["end"] is None else item["end"]

    clusters: list[dict[str, Any]] = []
    for item in highlights:
        current = clusters[-1] if clusters else None
        if current is not None and item["start"] - _extent(current) <= cluster_s:
            # A member with no end is still running, so the destination has no
            # end either -- synthesising one would assert an exit that hasn't
            # happened, the same reason events[].end stays null in /v1/reel.
            current["end"] = (
                None
                if current["end"] is None or item["end"] is None
                else max(current["end"], item["end"])
            )
            current["events"] += 1
            # The destination is described by its most confident member.
            if (item["score"] or -1.0) > (current["score"] or -1.0):
                current["score"] = item["score"]
                current["reason"] = item["reason"]
        else:
            clusters.append({**item, "events": 1})
    return clusters


@router.get("/highlights/{camera}")
async def highlights(
    camera: str,
    request: Request,
    before: float,
    limit: int = 10,
    order: str = "recent",
    cluster_s: float = 0.0,
) -> Any:
    """Index of interesting moments (§4.7), from `event` rows -- `reason` is a
    Frigate object label (person/car/package/...), the same vocabulary the
    client already maps to lanes.

    These are **raw events by default**, newest first: `limit` bounds the events
    considered, not the destinations returned, and one subject crossing the
    frame produces several. Two opt-in knobs, both off by default so an existing
    consumer sees no change:

    * `cluster_s` groups events within that many seconds of each other into one
      destination (`events` counts the members) -- what a "jump to the next
      interesting thing" control actually wants.
    * `order=score` ranks by peak confidence instead of recency. Recency stays
      the default because a client scanning for adjacency depends on time order.
    """
    settings = request.app.state.settings
    limit = max(1, min(limit, 100))
    if order not in ("recent", "score"):
        raise _bad_range("order must be 'recent' or 'score'")
    if cluster_s < 0:
        raise _bad_range("cluster_s must be >= 0")

    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        if camera not in _known_cameras(request.app.state, conn):
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_CAMERA_UNKNOWN, "message": f"no such camera: {camera}"},
            )
        rows = conn.execute(
            "SELECT * FROM event WHERE camera = ? AND start_time < ? "
            "ORDER BY start_time DESC LIMIT ?",
            (camera, before, limit),
        ).fetchall()
    finally:
        conn.close()

    items = [
        {
            "start": r["start_time"],
            "end": r["end_time"],
            "reason": r["label"],
            "score": db.event_top_score(r),
        }
        for r in rows
    ]
    # Clustering needs ascending order; the response is newest-first.
    items = _cluster_highlights(list(reversed(items)), cluster_s)
    items.reverse()
    if order == "score":
        items.sort(key=lambda h: (h["score"] if h["score"] is not None else -1.0), reverse=True)

    return {"highlights": items}
