"""Frigate-session gate for every endpoint the sidecar owns.

The sidecar has no user database and never holds a password: it validates the
client's own Frigate session cookie against Frigate's authenticated origin and
caches the pass for a short TTL. That is the same mechanism `/v1` already used
(docs/scrub-cache-and-proxy-spec.md §3.2 finding 5, option (a)); this module
generalises it so the triage UI, `/faces`, `/analysis` and `/toybox` are
covered too. Those endpoints expose event history, face crops of identified
people, and label/promote writes with side effects on Frigate itself, so
leaving them open made the sidecar a way around Frigate's own auth.

Deliberately NOT gated here:

* the reverse-proxy catch-all -- Frigate authenticates that traffic itself and
  its 401/`WWW-Authenticate` challenge has to reach the client intact;
* `/v1/capabilities`, `/healthz`, `/version` -- reachability probes a client
  needs *before* it has a session, and `/static`.

The gate is applied as raw ASGI middleware rather than a router dependency so
it can't be forgotten on a newly added route, and so it doesn't wrap the
proxy's streaming responses.
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.routing import Match

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from fastapi import FastAPI
    from starlette.routing import BaseRoute

    from frigate_sidecar.config import Settings

ERR_UNAUTHORIZED = "unauthorized"
ERR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"

# Reachability probes a client may need before it holds a Frigate session.
EXEMPT_PATHS = frozenset({"/healthz", "/version", "/v1/capabilities"})
EXEMPT_PREFIXES = ("/static/",)


def _cache(app: FastAPI) -> dict[str, float]:
    cache: dict[str, float] | None = getattr(app.state, "auth_cache", None)
    if cache is None:
        cache = {}
        app.state.auth_cache = cache
    return cache


def _remember(app: FastAPI, key: str, expiry: float, *, max_entries: int) -> None:
    """Record a validated session, evicting expired entries first.

    Frigate rotates its JWT, so the raw cookie is an unbounded key space: the
    cache has to be swept and capped or it is simply a slow memory leak.
    """
    cache = _cache(app)
    now = time.time()
    if len(cache) >= max_entries:
        for k in [k for k, exp in cache.items() if exp <= now]:
            del cache[k]
    while len(cache) >= max_entries:
        oldest = min(cache, key=lambda k: cache[k])
        del cache[oldest]
    cache[key] = expiry


async def validate_frigate_session(app: FastAPI, cookie: str) -> None:
    """Raise HTTPException unless `cookie` is a live Frigate session.

    Validated against `frigate.proxy_base_url` -- the authenticated origin --
    because that is the one that can actually reject a bad cookie.
    """
    from frigate_sidecar.frigate_api import get_async_client

    settings: Settings = app.state.settings
    if not cookie:
        raise HTTPException(status_code=401, detail="frigate session required")

    key = hashlib.sha256(cookie.encode()).hexdigest()
    now = time.time()
    expiry = _cache(app).get(key)
    if expiry is not None and expiry > now:
        return

    upstream = f"{settings.frigate.proxy_base_url.rstrip('/')}/api/version"
    client = get_async_client(app)
    try:
        resp = await client.get(upstream, headers={"cookie": cookie}, timeout=5.0)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"{ERR_UPSTREAM_UNAVAILABLE}: {exc}"
        ) from exc

    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="frigate session invalid")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=ERR_UPSTREAM_UNAVAILABLE)

    _remember(
        app,
        key,
        now + settings.sidecar.auth_cache_ttl_s,
        max_entries=settings.sidecar.auth_cache_max_entries,
    )


class FrigateAuthMiddleware:
    """Require a Frigate session on the sidecar's own routes.

    `owned_routes` is everything registered before the proxy catch-all, so a
    path the sidecar doesn't serve itself falls through to Frigate untouched.
    """

    def __init__(self, app: Any, *, owned_routes: Iterable[BaseRoute]) -> None:
        self.app = app
        self._owned = list(owned_routes)

    def _owns(self, scope: dict[str, Any]) -> bool:
        for route in self._owned:
            match, _ = route.matches(scope)
            # PARTIAL means the path matched but the method didn't: still ours,
            # and a 405 shouldn't be answered by Frigate either.
            if match in (Match.FULL, Match.PARTIAL):
                return True
        return False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # WebSocket scopes are gated too. No sidecar-owned WS route exists today
        # -- `_owns` matches none of them, so every upgrade falls through to the
        # proxy and Frigate authenticates it -- but skipping the whole scope
        # type meant the first one added would have been unauthenticated by
        # default, with nothing in the code saying so.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        app: FastAPI = scope["app"]
        settings: Settings = app.state.settings
        path: str = scope.get("path", "")
        if (
            not settings.sidecar.require_frigate_auth
            or path in EXEMPT_PATHS
            or path.startswith(EXEMPT_PREFIXES)
            or not self._owns(scope)
        ):
            await self.app(scope, receive, send)
            return

        cookie = ""
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie = value.decode("latin-1")
                break

        try:
            await validate_frigate_session(app, cookie)
        except HTTPException as exc:
            if scope["type"] == "websocket":
                # Reject before the handshake completes; 1008 is "policy
                # violation", which is what a client sees for an auth failure.
                await send({"type": "websocket.close", "code": 1008})
                return
            body = {
                "error": ERR_UNAUTHORIZED if exc.status_code == 401 else ERR_UPSTREAM_UNAVAILABLE,
                "message": str(exc.detail),
            }
            response = JSONResponse(body, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
