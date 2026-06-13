"""Wildlife-cam gallery: a viewer for the PoE trail-camera on the Pi.

Deliberately unrelated to Frigate analysis (like `toybox`) — it surfaces the
stills and PIR motion events produced by a separate project
(`helicopterrun/wildlife-cam`, a FastAPI backend on a Raspberry Pi at
192.168.1.37:8000).

The page is otherwise fully client-side: it fetches the wildlife API through a
same-origin reverse-proxy prefix (default ``/wildlifecam/``) configured in Nginx
Proxy Manager, which injects the ``X-API-Token`` for the mutating control
endpoints, so no token ever ships in our JS.

The ONE server-side exception is event-clip playback (``wildlife_media`` below).
Day events carry an ``.mp4`` under the Pi's open ``/media/{path}`` endpoint, but
that path isn't reliably forwarded by the NPM ``/wildlifecam`` location, so the
``<video>`` would 404. We stream those clips through the sidecar instead — media
is an open read endpoint upstream, so no token is involved. ``static/js/
wildlife.js`` does everything else. See the wildlife-cam repo's ``docs/API.md``
for the consumed contract.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter(tags=["wildlife"])

# Browser-facing prefix (NPM → Pi, with token injection) used by the page JS.
# Override per-request with ?api=<base> for LAN-direct testing (read endpoints
# are open; controls need the proxy's token injection).
_API_BASE = "/wildlifecam"

# Direct LAN address of the wildlife-cam backend, used ONLY for the server-side
# clip proxy below. Hardcoded like the rest of this integration (the Pi has a
# static lease and no Tailscale/DNS name that resolves from the LXC).
_PI_BASE = "http://192.168.1.37:8000"

# Request headers forwarded upstream (range → <video> seeking) and response
# headers passed back (content metadata the player needs).
_REQ_PASS = ("range",)
_RESP_PASS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
)


@router.get("/wildlife", response_class=HTMLResponse)
def wildlife_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "wildlife.html",
        {"api_base": _API_BASE, "counts": {}},
    )


@router.get("/wildlife/media/{path:path}")
async def wildlife_media(path: str, request: Request) -> Any:
    """Stream a clip/media file from the Pi's open ``/media/{path}`` endpoint.

    Routed through the sidecar so ``<video>`` playback doesn't depend on the NPM
    ``/wildlifecam/media`` forwarding. ``Range`` passes through so the browser
    can seek, and the upstream status (200 / 206 / 404) is mirrored.
    """
    # Defense-in-depth: the {path:path} capture could contain traversal. The Pi
    # resolves it under its own /media root regardless, but reject it here too.
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="bad path")

    upstream = f"{_PI_BASE}/media/{path}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() in _REQ_PASS}

    # No read timeout — we're streaming a video the user may pause/seek.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        req = client.build_request("GET", upstream, headers=fwd)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    if resp.status_code >= 400:
        code = resp.status_code
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=code, detail="clip unavailable")

    headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

    async def body() -> Any:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=resp.status_code,
        headers=headers,
        media_type=resp.headers.get("content-type"),
    )
