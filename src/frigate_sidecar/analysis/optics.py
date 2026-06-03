"""Optics & placement math: focal length -> HFOV -> object pixels at distance.

Pure functions (no I/O) plus camera-model, lens, and object presets, so the
placement-planner page (`routes/placement.py` + `static/js/placement.js`) and
its tests share one source of truth. The JS re-implements the same formulas
client-side for live interactivity; `tests/test_optics.py` pins the Python
side so the two can't silently drift.

Model notes
-----------
* HFOV is a rectilinear pinhole model: ``HFOV = 2*atan(W / (2*f))``. Real
  wide-angle lenses distort a few degrees wider than this, so deployed cameras
  carry a *measured* HFOV that overrides the computed value in the UI.
* Varifocal lenses are calibrated to a two-point fit (wide + tele datasheet
  anchors) via :func:`fit_varifocal`, which solves an effective sensor width
  ``W`` and focal offset ``f0`` so the curve passes through *both* anchors
  exactly. This is more accurate across the zoom range than a single nominal
  sensor width, because published CCTV FOV figures bake in some distortion.
* Pixel math keys on the object's horizontal *width*; ``area_px2`` is derived
  as ``width_px**2 * aspect`` (aspect = height/width) so it is directly
  comparable to Frigate's ``objects.filters.<obj>.min_area``.
"""

from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------
# Core geometry
# --------------------------------------------------------------------------


def hfov_from_focal(focal_mm: float, sensor_width_mm: float, focal_offset_mm: float = 0.0) -> float:
    """Horizontal field of view in degrees for a rectilinear lens.

    ``focal_offset_mm`` is the fitted offset for varifocal lenses (0 for an
    ideal pinhole). See :func:`fit_varifocal`.
    """
    return math.degrees(2 * math.atan(sensor_width_mm / (2 * (focal_mm + focal_offset_mm))))


def focal_from_hfov(hfov_deg: float, sensor_width_mm: float, focal_offset_mm: float = 0.0) -> float:
    """Inverse of :func:`hfov_from_focal` — focal length that yields ``hfov_deg``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return sensor_width_mm / (2 * half) - focal_offset_mm


def fit_varifocal(anchors: list[tuple[float, float]]) -> tuple[float, float]:
    """Solve ``(W, f0)`` so ``HFOV(f) = 2*atan(W / (2*(f + f0)))`` passes through
    both ``(focal_mm, hfov_deg)`` anchor points exactly.

    Returns ``(sensor_width_mm, focal_offset_mm)``.
    """
    (f1, h1), (f2, h2) = anchors
    t1 = math.tan(math.radians(h1) / 2)
    t2 = math.tan(math.radians(h2) / 2)
    # W = 2*t1*(f1 + f0) = 2*t2*(f2 + f0)  ->  f0*(t1 - t2) = t2*f2 - t1*f1
    f0 = (t2 * f2 - t1 * f1) / (t1 - t2)
    w = 2 * t1 * (f1 + f0)
    return w, f0


def px_per_ft(det_w_px: int, hfov_deg: float, distance_ft: float) -> float:
    """Horizontal pixels covering one foot at ``distance_ft``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return det_w_px / (2 * distance_ft * half)


def object_px_width(
    real_width_ft: float, det_w_px: int, hfov_deg: float, distance_ft: float
) -> float:
    """On-screen width in pixels of an object ``real_width_ft`` wide at ``distance_ft``."""
    return real_width_ft * px_per_ft(det_w_px, hfov_deg, distance_ft)


def max_distance_ft(
    real_width_ft: float, det_w_px: int, hfov_deg: float, target_px: float
) -> float:
    """Farthest distance (ft) at which the object's width still meets ``target_px``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return real_width_ft * det_w_px / (2 * target_px * half)


def target_area_px2(target_px_width: float, aspect_h_to_w: float) -> float:
    """Bounding-box area in px^2 for a ``target_px_width``-wide box of given aspect.

    Comparable to Frigate ``objects.filters.<obj>.min_area``.
    """
    return target_px_width * target_px_width * aspect_h_to_w


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

# Lens / camera-model catalogue. Varifocal lenses give two datasheet anchors
# (wide, tele); fixed lenses give a single nominal HFOV. ``custom`` lets the
# user drive HFOV directly. Resolutions are offered separately so a model can
# be evaluated at either its detect-substream or main-stream width.
_LENSES: list[dict[str, Any]] = [
    {
        "id": "dahua-5442-vf",
        "label": "Dahua/EmpireTech 5442 varifocal 2.7–12mm (1/1.8\")",
        "type": "varifocal",
        "focal_min": 2.7,
        "focal_max": 12.0,
        # Datasheet horizontal FOV: 114° @ 2.7mm, 47° @ 12mm.
        "anchors": [[2.7, 114.0], [12.0, 47.0]],
        "note": "ZEB fleet: gate, garden, alley-wide, stairway-tight, stairway-wide.",
    },
    {
        "id": "dahua-5442-fixed28",
        "label": "Dahua 5442 fixed 2.8mm (1/1.8\")",
        "type": "fixed",
        "hfov": 108.0,
        "note": "walkway (HDW5442TP-AS). Lens mm unconfirmed — measure to verify.",
    },
    {
        "id": "dahua-t2431-fixed28",
        "label": "Dahua T2431 fixed 2.8mm (1/2.7\")",
        "type": "fixed",
        "hfov": 106.0,
        "note": "street. Deployed instance measures ~115°.",
    },
    {
        "id": "unifi-g4-doorbell",
        "label": "UniFi G4 Doorbell Pro",
        "type": "fixed",
        "hfov": 100.0,
    },
    {
        "id": "unifi-g4-instant",
        "label": "UniFi G4 Instant",
        "type": "fixed",
        "hfov": 102.0,
        "note": "Wide native lens; deployed `package` is aimed/cropped to a tighter cone (~55°).",
    },
    {
        "id": "unifi-g5-flex",
        "label": "UniFi G5 Flex",
        "type": "fixed",
        "hfov": 102.0,
        "note": "crows-nest deployed instance measures ~70°.",
    },
    {
        "id": "custom",
        "label": "Custom (enter HFOV directly)",
        "type": "custom",
    },
]

# Common detect/main stream widths in the fleet.
_RESOLUTIONS: list[dict[str, Any]] = [
    {"id": "sub-720", "label": "1280×720 (detect substream)", "w": 1280, "h": 720},
    {"id": "main-1520", "label": "2688×1520 (Dahua main)", "w": 2688, "h": 1520},
    {"id": "fhd-1080", "label": "1920×1080 (street main)", "w": 1920, "h": 1080},
    {"id": "doorbell-720", "label": "960×720 (doorbell)", "w": 960, "h": 720},
    {"id": "main-960", "label": "1280×960 (package 4:3 main)", "w": 1280, "h": 960},
]

# Object catalogue: real_width_ft, aspect (height/width), and a target minimum
# on-screen width in px for reliable detection/recognition. Rules of thumb —
# tune against real event data later (frigate.db calibration is a follow-up).
_OBJECTS: list[dict[str, Any]] = [
    {"id": "face", "label": "face (recognition)", "width_ft": 0.5, "aspect": 1.4, "target_px": 80},
    {"id": "person", "label": "person", "width_ft": 1.6, "aspect": 3.4, "target_px": 40},
    {"id": "car", "label": "car (front/rear)", "width_ft": 6.0, "aspect": 0.8, "target_px": 50},
    {"id": "bicycle", "label": "bicycle + rider", "width_ft": 1.8, "aspect": 1.2, "target_px": 45},
    {"id": "motorcycle", "label": "motorcycle", "width_ft": 2.0, "aspect": 1.3, "target_px": 45},
    {"id": "package", "label": "package", "width_ft": 1.0, "aspect": 0.9, "target_px": 30},
    {"id": "cat", "label": "cat / raccoon", "width_ft": 1.3, "aspect": 0.55, "target_px": 35},
]

# Deployed fleet. ``hfov`` is the *measured* value (from the spatial reference);
# ``focal_mm`` is back-solved for varifocal cams so the zoom slider lands at the
# right spot when a camera is picked. ``res_id`` selects the operative detect
# stream. Mount height / facing carried for context.
_CAMERAS: list[dict[str, Any]] = [
    {"id": "street", "lens": "dahua-t2431-fixed28", "hfov": 115, "res": "fhd-1080",
     "mount_ft": 35, "faces": "S", "face_rec": False, "note": "high mount, steep angle"},
    {"id": "gate", "lens": "dahua-5442-vf", "hfov": 80, "res": "sub-720",
     "mount_ft": 10, "faces": "SE", "face_rec": False},
    {"id": "garden", "lens": "dahua-5442-vf", "hfov": 80, "res": "sub-720",
     "mount_ft": 7, "faces": "SW", "face_rec": False},
    {"id": "doorbell", "lens": "unifi-g4-doorbell", "hfov": 100, "res": "doorbell-720",
     "mount_ft": 3, "faces": "S", "face_rec": True},
    {"id": "package", "lens": "unifi-g4-instant", "hfov": 55, "res": "main-960",
     "mount_ft": 3, "faces": "S", "face_rec": False, "note": "tight aimed cone"},
    {"id": "alley-wide", "lens": "dahua-5442-vf", "hfov": 115, "res": "sub-720",
     "mount_ft": 45, "faces": "N", "face_rec": False, "note": "high mount, steep angle"},
    {"id": "stairway-tight", "lens": "dahua-5442-vf", "hfov": 90, "res": "sub-720",
     "mount_ft": 10, "faces": "N", "face_rec": True},
    {"id": "stairway-wide", "lens": "dahua-5442-vf", "hfov": 90, "res": "sub-720",
     "mount_ft": 10, "faces": "NW", "face_rec": False},
    {"id": "walkway", "lens": "dahua-5442-fixed28", "hfov": 108, "res": "sub-720",
     "mount_ft": 10, "faces": "E", "face_rec": False, "note": "HFOV estimated — confirm"},
    {"id": "crows-nest", "lens": "unifi-g5-flex", "hfov": 70, "res": "sub-720",
     "mount_ft": 55, "faces": "W", "face_rec": False, "note": "animal-only; high mount"},
]


def _lens_with_fit(lens: dict[str, Any]) -> dict[str, Any]:
    """Attach fitted ``W``/``f0`` to a varifocal lens so JS can evaluate HFOV(f)."""
    out = dict(lens)
    if lens["type"] == "varifocal":
        w, f0 = fit_varifocal([tuple(a) for a in lens["anchors"]])
        out["sensor_width_mm"] = round(w, 4)
        out["focal_offset_mm"] = round(f0, 4)
    return out


def presets_payload() -> dict[str, Any]:
    """JSON-serializable bundle injected into the placement template for the JS."""
    return {
        "lenses": [_lens_with_fit(lo) for lo in _LENSES],
        "resolutions": _RESOLUTIONS,
        "objects": _OBJECTS,
        "cameras": _CAMERAS,
    }
