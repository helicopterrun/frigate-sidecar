"""Liveness/version endpoints.

/healthz reflects background-task liveness, not just process liveness: the
MQTT subscriber once died silently and stayed down for 41 hours while the
static "ok" healthcheck kept reporting healthy (server.py `_push_subscriber_loop`
docstring). A degraded check returns 503 so the Docker/compose healthchecks
(which treat any non-2xx as unhealthy) and plain `curl -f` both notice.

Frigate reachability is checked too (`checks["frigate"]`), but -- unlike
mqtt/db/scrub/face_enrich -- it never flips the status code. `watchdog.py`
already probes Frigate directly and restarts *that* container when it hangs;
a 503 here would instead get the *sidecar* restarted by systemd/Docker,
which fixes nothing and duplicates recovery logic in the wrong process. See
`_probe_frigate` for detail.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from frigate_sidecar import __version__, db

router = APIRouter(tags=["meta"])

# A scrub cycle that hasn't finished in this many ticks is stuck, not slow --
# the loop is deadline-based, so healthy cycles land every tick even when the
# cache is cold. Generous on purpose: a restart storm is worse than a late alarm.
_SCRUB_STALE_TICKS = 10

# Frigate reachability probe: cached at app-state level so /healthz polls
# (Docker/systemd hit this every few seconds) don't hammer Frigate with an
# extra request on top of everything else already probing it.
_FRIGATE_PROBE_INTERVAL_S = 30.0
_FRIGATE_PROBE_TIMEOUT_S = 3.0


def _probe_frigate(app: Any, settings: Any, now: float) -> str:
    """Cheap `/api/version` reachability check, rate-limited per app instance.

    Deliberately does NOT feed into /healthz's overall `ok`/status code.
    `watchdog.py` already probes Frigate directly and restarts the Frigate
    container itself when it hangs; that is the correct recovery action.
    Flipping /healthz to 503 here would instead prompt systemd/Docker to
    restart the *sidecar*, which does nothing to fix Frigate and just adds a
    second, redundant (and wrong) recovery path. This check exists so a
    Frigate outage is visible in the sidecar's own health output, not to
    trigger sidecar restarts.
    """
    cache = getattr(app.state, "_frigate_health_cache", None)
    if cache is not None and (now - cache[0]) < _FRIGATE_PROBE_INTERVAL_S:
        return cache[1]  # type: ignore[no-any-return]
    url = settings.frigate.base_url.rstrip("/") + "/api/version"
    try:
        resp = httpx.get(url, timeout=_FRIGATE_PROBE_TIMEOUT_S)
        status = "ok" if resp.status_code == 200 else "error"
    except httpx.HTTPError:
        status = "unreachable"
    app.state._frigate_health_cache = (now, status)
    return status


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    app = request.app
    settings = app.state.settings
    now = time.time()
    checks: dict[str, Any] = {}
    ok = True

    # Informational only -- see `_probe_frigate` docstring for why this does
    # not gate the status code.
    checks["frigate"] = _probe_frigate(app, settings, now)

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

    scrub_low_disk: bool | None = None
    if settings.scrub.enabled:
        scrub_low_disk = bool(getattr(app.state, "scrub_low_disk", False))
        if getattr(app.state, "scrub_locked", False):
            # Another process (a restarting predecessor, or a concurrent CLI
            # `fsc scrub` invocation) holds the cache lock -- the generation
            # loop was never started. Distinct from "stale" (a loop that
            # started and died) so an operator knows to check for a stray
            # process rather than a wedge.
            checks["scrub"] = "locked"
            ok = False
        else:
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

    body: dict[str, Any] = {"status": "ok" if ok else "degraded", "checks": checks}
    if scrub_low_disk is not None:
        body["scrub_low_disk"] = scrub_low_disk
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}
