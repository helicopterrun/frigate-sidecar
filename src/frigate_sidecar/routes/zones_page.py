"""/zones moved into the unified /settings page (2026-08); redirect kept so
bookmarks and older app links keep working."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["zones"])


@router.get("/zones")
def zones_view() -> Any:
    return RedirectResponse("/settings#zones", status_code=308)
