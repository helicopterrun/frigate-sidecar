"""Map page: place/move/aim cameras on the layout map (optionally over an
uploaded floorplan), draw the secure area, calibrate scale and optics
(landmark solve, auto-tune). CAD-style rebuild of the original cameras page
(docs/cameras-page-rebuild-notes.md); movement heading derives from the map
geometry (pie azimuth + secure area) automatically."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from frigate_sidecar.analysis import optics

router = APIRouter(tags=["map"])


@router.get("/map", response_class=HTMLResponse)
def map_view(request: Request) -> Any:
    templates = request.app.state.templates
    # The lens catalogue prefills HFOV from datasheet numbers in the
    # inspector's lens select.
    presets = optics.presets_payload()
    return templates.TemplateResponse(
        request, "map.html",
        {"lens_presets_json": json.dumps(presets["lenses"])},
    )


@router.get("/cameras", include_in_schema=False)
@router.get("/cameras2", include_in_schema=False)
def cameras_alias() -> Any:
    """Old names for this page (and its side-by-side rebuild alias)."""
    return RedirectResponse("/map", status_code=301)
