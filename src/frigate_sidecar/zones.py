"""Read Frigate camera/zone definitions from `config.yml` for UI overlays."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

ZONE_PALETTE = (
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899",
    "#14b8a6", "#eab308", "#22d3ee", "#f97316", "#84cc16", "#a855f7",
)


def _parse_coords(coords: object) -> list[tuple[float, float]] | None:
    if not coords:
        return None
    try:
        nums = [float(x) for x in str(coords).split(",")]
    except ValueError:
        return None
    if len(nums) < 6 or len(nums) % 2 != 0:
        return None
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


def is_full_frame(coords: list[tuple[float, float]], eps: float = 0.02) -> bool:
    """A polygon that spans the entire frame (e.g. per-camera `animal` zones)."""
    if len(coords) != 4:
        return False
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return (
        min(xs) < eps
        and max(xs) > 1 - eps
        and min(ys) < eps
        and max(ys) > 1 - eps
    )


def color_for_zone(name: str) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return ZONE_PALETTE[h % len(ZONE_PALETTE)]


def load_camera_zones(config_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Return `{camera: [{name, coords, color, objects, inertia, loitering_time}, ...]}`.

    Reads Frigate's config.yml. Missing file or unparseable file -> {} (no crash).
    """
    p = Path(config_path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for cam_name, cam in (cfg.get("cameras") or {}).items():
        if not isinstance(cam, dict):
            continue
        cam_zones: list[dict[str, Any]] = []
        for zname, z in (cam.get("zones") or {}).items():
            zd = z or {}
            pts = _parse_coords(zd.get("coordinates"))
            if not pts:
                continue
            objs = zd.get("objects")
            if isinstance(objs, str):
                objs = [objs]
            cam_zones.append(
                {
                    "name": zname,
                    "coords": pts,
                    "color": color_for_zone(zname),
                    "objects": objs or [],
                    "inertia": zd.get("inertia"),
                    "loitering_time": zd.get("loitering_time"),
                }
            )
        if cam_zones:
            out[cam_name] = cam_zones
    return out
