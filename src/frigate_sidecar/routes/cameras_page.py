"""Camera calibration page: onboard cameras (lens/FOV, mount height, tilt,
direction), draw a per-camera "toward home" vector on the live snapshot
(drives the LA heading chip), and arrange cameras on the layout map —
optionally over an uploaded floorplan."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar.analysis import optics

router = APIRouter(tags=["cameras"])


@router.get("/cameras", response_class=HTMLResponse)
def cameras_view(request: Request) -> Any:
    templates = request.app.state.templates
    # The onboarding form offers the placement page's lens catalogue
    # (fixed -> HFOV prefilled, varifocal -> focal slider via the fitted
    # sensor width) so a new camera starts from datasheet numbers.
    presets = optics.presets_payload()
    return templates.TemplateResponse(
        request, "cameras.html",
        {"lens_presets_json": json.dumps(presets["lenses"])},
    )


@router.get("/cameras2", response_class=HTMLResponse)
def cameras_edit_view(request: Request) -> Any:
    """CAD-style rebuild of the map editor (docs/cameras-page-rebuild-notes.md).

    Lives at /cameras2 while the rebuild is verified side-by-side; the final
    commit points /cameras here and retires the legacy page."""
    templates = request.app.state.templates
    presets = optics.presets_payload()
    return templates.TemplateResponse(
        request, "cameras_edit.html",
        {"lens_presets_json": json.dumps(presets["lenses"])},
    )
