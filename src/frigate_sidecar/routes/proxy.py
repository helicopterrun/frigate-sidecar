"""Transparent reverse proxy to Frigate's authenticated origin.

Gives Elsinore one base URL: everything the sidecar doesn't handle itself
(``/api/*``, ``/vod/*``, ``/live/*``, ``/preview/*``, and any other Frigate
path) streams through to Frigate's authed port unchanged, with ``Range`` and
the client's own auth cookie/header passed through untouched. Auth stays
entirely Frigate's -- the sidecar never holds or validates the password.

Registered LAST in server.py so ``/v1/*``, ``/static``, the sidecar's own
pages, and ``/healthz`` all win first; only unmatched paths fall through here.

Near-copy of the streaming-proxy pattern in ``routes/wildlife.py``
(``wildlife_media``), generalised: forwards more headers (auth, not just
range), mirrors more statuses (401 in particular -- Frigate's auth challenge
must reach the client intact), and passes the HTTP method through instead of
being GET-only (POST /api/reviews/viewed and exports need it).
See docs/scrub-cache-and-proxy-spec.md §6.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["proxy"])

_RESP_PASS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
    "etag",
    "set-cookie",
    "www-authenticate",
)


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_passthrough(path: str, request: Request) -> Any:
    settings = request.app.state.settings
    if not settings.proxy.enabled:
        raise HTTPException(status_code=404, detail="proxy disabled")

    # `/v1` is a namespace reserved entirely for the sidecar's own endpoints
    # (docs/scrub-cache-and-proxy-spec.md §4.0) -- an unmatched /v1/* path
    # must JSON-404 here, never fall through to Frigate (which could 200 it
    # with its SPA shell, or the upstream could simply be unreachable and
    # return a confusing 502 for what is really a 404).
    if path == "v1" or path.startswith("v1/"):
        raise HTTPException(
            status_code=404, detail={"error": "not_generated", "message": "unknown /v1 path"}
        )

    # Defense-in-depth: FastAPI's router already resolves ".." segments before
    # matching, but reject explicitly too (matches wildlife.py's guard).
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="bad path")

    upstream = f"{settings.frigate.proxy_base_url.rstrip('/')}/{path}"
    if request.url.query:
        upstream = f"{upstream}?{request.url.query}"

    pass_headers = {h.lower() for h in settings.proxy.pass_request_headers}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in pass_headers}

    body = await request.body()

    # No read timeout on media -- VOD/live are long-lived streams the user
    # pauses and seeks (matches wildlife.py::wildlife_media).
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        req = client.build_request(
            request.method, upstream, headers=fwd_headers, content=body or None
        )
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

    async def stream_body() -> Any:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=headers,
        media_type=resp.headers.get("content-type"),
    )
