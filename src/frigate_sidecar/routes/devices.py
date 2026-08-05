"""Push-devices admin page: registered devices + their filters + test sends.

Read-only listing over `push/store.py`; the Test button reuses the existing
`POST /v1/push/devices/{token}/test` endpoint, so behaviour (404/502/503
semantics) stays identical to what the iOS client gets.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar import db
from frigate_sidecar.push import store

router = APIRouter(tags=["devices"])


@router.get("/devices", response_class=HTMLResponse)
def devices_view(request: Request) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        devices = store.list_devices(conn)
    finally:
        conn.close()

    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "devices": devices,
            "push_enabled": settings.push.enabled,
            "transport": settings.push.transport,
            "counts": {},
        },
    )
