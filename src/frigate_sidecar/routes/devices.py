"""/devices moved into the unified /settings page (2026-08); redirect kept
so bookmarks keep working. The device table itself is rendered by
settings_page.py; the Test button still posts to
`POST /v1/push/devices/{token}/test`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["devices"])


@router.get("/devices")
def devices_redirect() -> Any:
    return RedirectResponse("/settings#push", status_code=308)
