"""Status dashboard: the landing page for a distributed install.

`GET /` renders the page server-side; `GET /status.json` returns the same
payload for the page's ~10s refresh loop. Deliberately NOT under /v1 -- the
iOS capabilities contract stays untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar import __version__, db

router = APIRouter(tags=["status"])

_PROBE_TIMEOUT_S = 3.0


def _file_size(path: object) -> int | None:
    try:
        return os.stat(str(path)).st_size
    except OSError:
        return None


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


async def _probe_frigate(base_url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/version")
            resp.raise_for_status()
            return {"reachable": True, "version": resp.text.strip()}
    except Exception as exc:  # noqa: BLE001 -- any failure is just "unreachable"
        return {"reachable": False, "error": str(exc)}


def _scrub_status(settings: Any, now: float, last_cycle: float | None) -> dict[str, Any]:
    scrub = {
        "enabled": settings.scrub.enabled,
        "last_cycle_s_ago": (round(now - last_cycle, 1) if last_cycle else None),
        "cameras": [],
        "cache_bytes": None,
    }
    if not settings.scrub.enabled:
        return scrub
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        cams = [
            r["camera"]
            for r in conn.execute("SELECT DISTINCT camera FROM scrub_buckets ORDER BY camera")
        ]
        exclude = tuple(settings.scrub.derived_intervals_s)
        for cam in cams:
            through = db.latest_generated_through(conn, cam, exclude_intervals_s=exclude)
            scrub["cameras"].append(
                {
                    "camera": cam,
                    "lag_s": (round(now - through, 1) if through else None),
                }
            )
        row = conn.execute("SELECT COUNT(*) AS n FROM scrub_sheets").fetchone()
        scrub["sheet_count"] = int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 -- schema may predate these tables
        pass
    finally:
        conn.close()
    scrub["cache_bytes"] = _dir_size_capped(settings.scrub.cache_dir)
    return scrub


def _dir_size_capped(root: object, max_files: int = 50_000) -> int | None:
    """Total bytes under `root`, bailing out (None) past `max_files` so a
    pathological cache can't stall the status page."""
    total = 0
    seen = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(str(root)):
            for name in filenames:
                seen += 1
                if seen > max_files:
                    return None
                with contextlib.suppress(OSError):
                    total += os.stat(os.path.join(dirpath, name)).st_size
    except OSError:
        return None
    return total


def _push_status(app_state: Any, settings: Any) -> dict[str, Any]:
    push: dict[str, Any] = {
        "enabled": settings.push.enabled,
        "transport": settings.push.transport,
        "device_count": 0,
        "mqtt_connected": None,
        "frigate_online": None,
        "last_event_s_ago": None,
    }
    with contextlib.suppress(Exception):
        conn = db.open_sidecar(settings.sidecar.db_path)
        try:
            from frigate_sidecar.push import store

            push["device_count"] = len(store.list_devices(conn))
        finally:
            conn.close()
    subscriber = getattr(app_state, "push_subscriber", None)
    if subscriber is not None:
        push["mqtt_connected"] = not subscriber.is_stale()
        push["frigate_online"] = subscriber.frigate_online
        push["last_event_s_ago"] = round(time.time() - subscriber.last_seen, 1)
    return push


async def _gather_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    now = time.time()
    frigate_task = asyncio.create_task(_probe_frigate(settings.frigate.base_url))
    scrub = await asyncio.to_thread(
        _scrub_status, settings, now, getattr(request.app.state, "scrub_last_cycle", None)
    )
    push = await asyncio.to_thread(_push_status, request.app.state, settings)
    return {
        "version": __version__,
        "time": now,
        "frigate": await frigate_task,
        "proxy_enabled": settings.proxy.enabled,
        "scrub": scrub,
        "push": push,
        "sizes": {
            "sidecar_db": _file_size(settings.sidecar.db_path),
            "frigate_db": _file_size(settings.frigate.db_path),
            "scrub_cache": scrub.get("cache_bytes"),
        },
    }


@router.get("/status.json")
async def status_json(request: Request) -> dict[str, Any]:
    return await _gather_status(request)


@router.get("/", response_class=HTMLResponse)
async def status_page(request: Request) -> Any:
    templates = request.app.state.templates
    status = await _gather_status(request)
    return templates.TemplateResponse(
        request,
        "status.html",
        {"status": status, "fmt_bytes": _fmt_bytes, "counts": {}},
    )
