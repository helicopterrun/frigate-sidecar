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

import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from frigate_sidecar import __version__, db

router = APIRouter(prefix="/v1", tags=["v1"])

# Error vocabulary (docs spec §4.0) -- always paired with a machine-readable
# `error` field so the client can distinguish "nothing here yet" from "broken".
_ERR_CAMERA_UNKNOWN = "camera_unknown"
_ERR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"

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
    return {
        "version": __version__,
        "scrub_cache": {
            "enabled": settings.scrub.enabled,
            "format": settings.scrub.format,
            "cameras": list(settings.scrub.cameras),
            "generated": False,  # no generator implemented yet
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
    return result
