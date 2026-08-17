"""Unified settings page, organized like the app's SettingsView.

Absorbs the old /zones (routing policy, neighbors, export/import — its
section markup keeps the same element ids so zones.js runs unchanged) and
/devices (registration table + test sends); adds a per-camera rig-facts
summary with landmark-calibrate deep links, read-only faces config, and the
about/debug block. /zones and /devices redirect here.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar import __version__, db
from frigate_sidecar.push import policy_settings, store
from frigate_sidecar.routes import scrub as scrub_routes
from frigate_sidecar.zones import load_camera_zones

router = APIRouter(tags=["settings"])


def _camera_summary(settings: Any) -> list[dict[str, Any]]:
    """Rig facts per camera: placed/aimed state + optics, for the summary
    table. Names come from the live Frigate config, like /cameras."""
    active = policy_settings.get_active()
    layout = active.get("camera_layout", {}) or {}
    optics = active.get("camera_optics", {}) or {}
    try:
        cameras = sorted(load_camera_zones(settings.frigate.config_path).keys())
    except Exception:  # noqa: BLE001 — a missing Frigate config is an empty list, not a 500
        cameras = sorted(set(layout) | set(optics))
    rows = []
    for cam in cameras:
        entry = layout.get(cam) or {}
        opt = optics.get(cam) or {}
        rows.append(
            {
                "camera": cam,
                "placed": "x" in entry and "y" in entry,
                "azimuth": entry.get("azimuth"),
                "fov": entry.get("fov"),
                "hfov": opt.get("hfov"),
                "mount_ft": opt.get("mount_ft"),
                "tilt_deg": opt.get("tilt_deg"),
            }
        )
    return rows


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        devices = store.list_devices(conn)
    finally:
        conn.close()

    caps = await scrub_routes.capabilities(request)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "devices": devices,
            "push_enabled": settings.push.enabled,
            "transport": settings.push.transport,
            "camera_rows": _camera_summary(settings),
            "face_auto_promote": settings.face.auto_promote,
            "face_quality_threshold": settings.face.quality_threshold,
            "version": __version__,
            "capabilities_json": json.dumps(caps, indent=2, sort_keys=True),
            "counts": {},
        },
    )
