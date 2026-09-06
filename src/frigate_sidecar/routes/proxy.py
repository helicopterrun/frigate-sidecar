"""Transparent reverse proxy to Frigate's authenticated origin.

Gives Elsinore one base URL: everything the sidecar doesn't handle itself
(``/api/*``, ``/vod/*``, ``/live/*``, ``/preview/*``, and any other Frigate
path) streams through to Frigate's authed port unchanged, with ``Range`` and
the client's own auth cookie/header passed through untouched. Auth stays
entirely Frigate's -- the sidecar never holds or validates the password, and
never applies its own session gate here (see auth.py).

Registered LAST in server.py so ``/v1/*``, ``/static``, the sidecar's own
pages, and ``/healthz`` all win first; only unmatched paths fall through here.

The body is relayed **raw**: httpx transparently decodes ``Content-Encoding``
when you iterate the decoded stream, so forwarding the upstream
``content-length`` alongside a decoded body produced a length that disagreed
with the bytes on the wire for every gzipped Frigate response. Streaming the
raw bytes and relaying ``content-encoding`` keeps the two consistent and is
what a transparent proxy should do anyway.

WebSockets are proxied too (``/ws`` for Frigate's state feed, go2rtc's WebRTC
signalling): an HTTP-only proxy silently broke live view for a client pointed
at the sidecar as its single origin.

See docs/scrub-cache-and-proxy-spec.md §6.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse

from frigate_sidecar.errors import error_detail
from frigate_sidecar.frigate_api import get_async_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["proxy"])

# Relayed verbatim from the upstream response. `content-encoding` belongs here
# because the body is streamed raw; `location` because a redirect without it is
# just a broken response.
_RESP_PASS = (
    "content-type",
    "content-length",
    "content-encoding",
    "content-range",
    "accept-ranges",
    "cache-control",
    "etag",
    "last-modified",
    "location",
    "www-authenticate",
)

_WS_SUBPROTOCOL_HEADER = "sec-websocket-protocol"

# This is the one route through the shared client (frigate_api.get_async_client)
# that legitimately needs an unbounded read: VOD/live media are long-lived
# streams the user pauses and seeks, so the client's own (now finite) default
# timeout would cut them off mid-view. Scoped to this one request rather than
# the shared client's default -- see frigate_api._DEFAULT_TIMEOUT.
_UPSTREAM_TIMEOUT = httpx.Timeout(30.0, read=None)

# `read=None` above means httpx itself will wait forever on a stalled chunk;
# this is what actually bounds that -- a chunk that doesn't arrive within this
# many idle seconds ends the response instead of holding the connection (and
# the client's wait) open indefinitely.
_IDLE_CHUNK_TIMEOUT_S = 30.0


def _upstream_url(settings: Any, path: str, query: str, *, scheme_ws: bool = False) -> str:
    base = settings.frigate.proxy_base_url.rstrip("/")
    if scheme_ws:
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
    url = f"{base}/{path}"
    return f"{url}?{query}" if query else url


def _reject_reserved(path: str) -> None:
    # `/v1` is a namespace reserved entirely for the sidecar's own endpoints
    # (docs/scrub-cache-and-proxy-spec.md §4.0) -- an unmatched /v1/* path
    # must JSON-404 here, never fall through to Frigate (which could 200 it
    # with its SPA shell, or the upstream could simply be unreachable and
    # return a confusing 502 for what is really a 404).
    if path == "v1" or path.startswith("v1/"):
        raise HTTPException(
            status_code=404, detail=error_detail("not_generated", "unknown /v1 path")
        )
    # Defense-in-depth: FastAPI's router already resolves ".." segments before
    # matching, but reject explicitly too (matches wildlife.py's guard).
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail=error_detail("bad_path", "bad path"))


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_passthrough(path: str, request: Request) -> Any:
    settings = request.app.state.settings
    if not settings.proxy.enabled:
        raise HTTPException(
            status_code=404, detail=error_detail("proxy_disabled", "proxy disabled")
        )

    _reject_reserved(path)

    upstream = _upstream_url(settings, path, request.url.query)

    pass_headers = {h.lower() for h in settings.proxy.pass_request_headers}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in pass_headers}
    # The client's own negotiation has to travel with the raw body we relay,
    # otherwise httpx substitutes its own and the response encoding no longer
    # matches what the client asked for.
    if "accept-encoding" in request.headers:
        fwd_headers["accept-encoding"] = request.headers["accept-encoding"]
    else:
        fwd_headers["accept-encoding"] = "identity"

    body = await request.body()

    client = get_async_client(request.app)
    try:
        req = client.build_request(
            request.method,
            upstream,
            headers=fwd_headers,
            content=body or None,
            timeout=_UPSTREAM_TIMEOUT,
        )
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", f"upstream error: {exc}")
        ) from exc

    headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

    # httpx hands back an already-read response in a few cases (redirect and
    # auth flows, and non-streaming transports). Its body is then decoded and
    # buffered, so the upstream framing headers no longer describe the bytes
    # we're about to send -- drop them and let Starlette frame it instead.
    buffered = getattr(resp, "is_stream_consumed", False)
    if buffered:
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)

    async def stream_body() -> Any:
        try:
            if buffered:
                yield resp.content
            else:
                chunks = resp.aiter_raw()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            chunks.__anext__(), timeout=_IDLE_CHUNK_TIMEOUT_S
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        logger.warning(
                            "proxy: idle read timeout (%.0fs) streaming %s; ending response",
                            _IDLE_CHUNK_TIMEOUT_S,
                            upstream,
                        )
                        break
                    yield chunk
        finally:
            await resp.aclose()

    response = StreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=headers,
        media_type=resp.headers.get("content-type"),
    )
    # Set-Cookie is the one header Frigate can legitimately send more than once
    # (login sets both the session and its refresh companion); reading it off
    # the mapping would comma-join them into a single malformed cookie.
    for value in _header_list(resp.headers, "set-cookie"):
        response.raw_headers.append((b"set-cookie", value.encode("latin-1")))
    return response


def _header_list(headers: Any, name: str) -> list[str]:
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        return list(get_list(name))
    value = headers.get(name)
    return [value] if value else []


#: (module, keyword for extra request headers) -- websockets moved `connect`
#: and renamed `extra_headers` to `additional_headers` in 14.x, and
#: uvicorn[standard] can pull either side of that split.
_WS_CLIENTS = (
    ("websockets.asyncio.client", "additional_headers"),
    ("websockets.client", "extra_headers"),
)


def _ws_connector() -> Any:
    """Return `await connect(url, headers, subprotocols)`, or None if unavailable."""
    import importlib

    for module_name, headers_kw in _WS_CLIENTS:
        try:
            connect = importlib.import_module(module_name).connect
        except (ImportError, AttributeError):
            continue

        async def _connect(
            url: str,
            headers: list[tuple[str, str]],
            subs: list[str],
            _connect: Any = connect,
            _kw: str = headers_kw,
        ) -> Any:
            return await _connect(
                url, **{_kw: headers}, subprotocols=subs or None, open_timeout=10
            )

        return _connect
    return None


@router.websocket("/{path:path}")
async def proxy_websocket(path: str, websocket: WebSocket) -> None:
    """Bidirectional WebSocket relay to Frigate (state feed, WebRTC signalling)."""
    settings = websocket.app.state.settings
    if not settings.proxy.enabled:
        await websocket.close(code=1008)
        return
    if path == "v1" or path.startswith("v1/") or ".." in path.split("/"):
        await websocket.close(code=1008)
        return

    connect = _ws_connector()
    if connect is None:  # pragma: no cover - uvicorn[standard] ships websockets
        logger.warning("proxy: websockets package unavailable; cannot relay %s", path)
        await websocket.close(code=1011)
        return

    upstream = _upstream_url(settings, path, websocket.url.query, scheme_ws=True)
    pass_headers = {h.lower() for h in settings.proxy.pass_request_headers}
    fwd_headers = [
        (k, v)
        for k, v in websocket.headers.items()
        if k.lower() in pass_headers and k.lower() != _WS_SUBPROTOCOL_HEADER
    ]
    subprotocols = websocket.scope.get("subprotocols") or []

    try:
        upstream_ws = await connect(upstream, fwd_headers, subprotocols)
    except Exception as exc:  # noqa: BLE001 - any handshake failure is a 1011 to the client
        logger.info("proxy: websocket connect to %s failed: %s", upstream, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream_ws.subprotocol)

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (data := message.get("text")) is not None:
                await upstream_ws.send(data)
            elif (raw := message.get("bytes")) is not None:
                await upstream_ws.send(raw)

    async def upstream_to_client() -> None:
        async for message in upstream_ws:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await upstream_ws.close()
        with contextlib.suppress(RuntimeError):
            await websocket.close()
