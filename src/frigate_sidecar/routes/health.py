"""Liveness/version endpoints.

/healthz reflects background-task liveness, not just process liveness: the
MQTT subscriber once died silently and stayed down for 41 hours while the
static "ok" healthcheck kept reporting healthy (server.py `_push_subscriber_loop`
docstring). A degraded check returns 503 so the Docker/compose healthchecks
(which treat any non-2xx as unhealthy) and plain `curl -f` both notice.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from frigate_sidecar import __version__, db

router = APIRouter(tags=["meta"])

# A scrub cycle that hasn't finished in this many ticks is stuck, not slow --
# the loop is deadline-based, so healthy cycles land every tick even when the
# cache is cold. Generous on purpose: a restart storm is worse than a late alarm.
_SCRUB_STALE_TICKS = 10


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    app = request.app
    settings = app.state.settings
    now = time.time()
    checks: dict[str, Any] = {}
    ok = True

    # `push_subscriber` is set by the lifespan when push starts; its absence
    # means the lifespan hasn't run (tests, bare create_app under another
    # runner), where "degraded" would be noise rather than signal.
    subscriber = getattr(app.state, "push_subscriber", None)
    if settings.push.enabled and subscriber is not None:
        connected = subscriber.connected
        checks["mqtt"] = "connected" if connected else "disconnected"
        # Disconnected is degraded even during startup/backoff: push is not
        # being delivered either way, and the reconnect loop clears it fast.
        ok = ok and connected

        try:
            conn = db.open_sidecar(str(settings.sidecar.db_path))
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
            checks["db"] = "ok"
        except Exception:
            checks["db"] = "error"
            ok = False

    if settings.scrub.enabled:
        tick = min(settings.scrub.generate_interval_s, settings.scrub.live_edge_interval_s)
        last_cycle = getattr(app.state, "scrub_last_cycle", None)
        started_at = getattr(app.state, "started_at", now)
        if last_cycle is not None:
            age = now - last_cycle
            checks["scrub_last_cycle_age_s"] = round(age, 1)
            if age > tick * _SCRUB_STALE_TICKS:
                checks["scrub"] = "stale"
                ok = False
            else:
                checks["scrub"] = "ok"
        elif now - started_at > tick * _SCRUB_STALE_TICKS:
            # Never completed a cycle and we're well past startup grace.
            checks["scrub"] = "stale"
            ok = False
        else:
            checks["scrub"] = "starting"

    if settings.face_enrich.enabled:
        # Same staleness shape as scrub. The worker stamps last_cycle only on
        # a completed run_cycle, so a wedged model load or a dead task both
        # read as stale here rather than as silence.
        tick = settings.face_enrich.interval_s
        last_cycle = getattr(app.state, "face_enrich_last_cycle", None)
        started_at = getattr(app.state, "started_at", now)
        if last_cycle is not None:
            age = now - last_cycle
            checks["face_enrich_last_cycle_age_s"] = round(age, 1)
            if age > tick * _SCRUB_STALE_TICKS:
                checks["face_enrich"] = "stale"
                ok = False
            else:
                checks["face_enrich"] = "ok"
        elif now - started_at > tick * _SCRUB_STALE_TICKS:
            checks["face_enrich"] = "stale"
            ok = False
        else:
            checks["face_enrich"] = "starting"

    body = {"status": "ok" if ok else "degraded", "checks": checks}
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}
