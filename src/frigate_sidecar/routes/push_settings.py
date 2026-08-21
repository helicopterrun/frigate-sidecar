"""`/v1/push/settings` -- the attention-ladder policy document, plus the
Frigate-config snapshot refresh that keeps its derived vocab (zones,
openings, cameras) honest on dev instances.

Split out of `routes/push.py`; same `/v1/push` prefix and auth.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from frigate_sidecar.push import policy_settings

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_INVALID_SETTINGS = "invalid_settings"
_ERR_STALE_REV = "stale_settings_rev"

# The settings document's revision guards against two tabs clobbering each
# other last-write-wins: a web client sends back the rev it loaded and gets a
# 409 if someone else saved in between. Clients that never send `rev` (the
# iOS app) keep the old behavior. The rev lives *in the settings file*
# (`policy_settings.read_rev`/`save_settings`) rather than process memory --
# an in-process counter reset to 1 on every restart, silently re-admitting
# any pre-deploy stale rev.


@router.get("/settings")
async def get_push_settings(request: Request) -> dict[str, Any]:
    """The attention-ladder policy (Elsinore Phase 4): the routing table,
    zone-class assignments, and Live Activity family toggles, plus enough
    about the live Frigate config (`available_zones`/`available_openings`)
    for the app to render its settings screens without a second call.

    Returns the *live, applied* policy (`policy_settings.get_active()`), not
    a fresh disk read -- they're the same thing once `startup`/a prior `PUT`
    has run, and this guarantees `GET` can never show something other than
    what the routing engine is actually evaluating against right now. On a
    fresh install with no settings file yet, this is what creates one (with
    defaults) rather than leaving `GET` and the on-disk state to silently
    disagree until the first `PUT`.
    """
    settings = request.app.state.settings
    active = policy_settings.get_active()
    settings_path = pathlib.Path(settings.push.push_settings_path)
    if not settings_path.exists():
        policy_settings.save_settings(settings_path, active)

    # Re-read friendly names on every GET (a cheap yaml load): editing
    # Frigate's config must show up here without a sidecar restart.
    policy_settings.load_zone_display_names(settings.frigate.config_path)

    from frigate_sidecar.zones import load_camera_zones

    available_cameras = sorted(load_camera_zones(settings.frigate.config_path).keys())
    derived_headings = {
        cam: vec for cam in available_cameras
        if (vec := policy_settings.derived_camera_heading(cam, active)) is not None
    }

    return {
        "settings": active,
        "rev": policy_settings.read_rev(settings_path),
        "available_cameras": available_cameras,
        "derived_headings": derived_headings,
        # Response key predates the settings-backed optics table; kept so
        # existing consumers (the app) need no change.
        "placement_deployments": {
            cam: dict(entry)
            for cam, entry in active.get("camera_optics", {}).items()
            if isinstance(entry, dict)
        },
        "available_zones": policy_settings.build_available_zones(settings.frigate.config_path),
        "available_openings": policy_settings.build_available_openings(
            settings.frigate.config_path
        ),
        "recognition_available": policy_settings.probe_recognition_available(
            settings.frigate.config_path
        ),
    }


@router.put("/settings")
async def put_push_settings(request: Request) -> dict[str, Any]:
    """Validate, persist, and immediately apply a new policy document.

    The body is the same shape `GET` returns under `settings` -- not the
    wrapper with `available_zones`/`available_openings`, which are derived,
    read-only, and never round-tripped back in. Unknown top-level fields are
    ignored (forward compat); an unknown subject/place/family key inside a
    known block, or an invalid level/place-class value, is a 400.
    """
    body = await request.json()
    settings = request.app.state.settings
    client_rev = body.pop("rev", None) if isinstance(body, dict) else None
    current_rev = policy_settings.read_rev(settings.push.push_settings_path)
    if isinstance(client_rev, int) and client_rev != current_rev:
        raise HTTPException(
            status_code=409,
            detail={
                "error": _ERR_STALE_REV,
                "detail": "Settings changed elsewhere — reload before saving.",
            },
        )
    errors = policy_settings.validate_settings(body)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID_SETTINGS, "detail": errors},
        )

    merged = policy_settings.normalize_settings(body)
    # la_only is sticky: normalize fills absent keys from *defaults*, and the
    # app's settings model round-trips through a fixed Codable type that drops
    # keys it doesn't know — so a client that omits la_only must not silently
    # reset it. Only an explicit boolean in the body changes it.
    la_body = body.get("live_activities") if isinstance(body, dict) else None
    active_la = policy_settings.get_active().get("live_activities", {})
    if not (isinstance(la_body, dict) and isinstance(la_body.get("la_only"), bool)):
        merged["live_activities"]["la_only"] = bool(active_la.get("la_only", False))
    if not (isinstance(la_body, dict) and la_body.get("delivery") in ("la_first", "notifications")):
        merged["live_activities"]["delivery"] = active_la.get("delivery", "la_first")
    # camera_neighbors / camera_headings / camera_layout are config-side only
    # (the app has no UI for them and its Codable round-trip drops the keys)
    # -- sticky unless explicitly sent.
    for sticky_key in (
        "camera_neighbors", "camera_headings", "camera_layout", "zone_names", "camera_optics",
    ):
        if not isinstance(body.get(sticky_key), dict):
            merged[sticky_key] = policy_settings.get_active().get(sticky_key, {})
    # secure_area / map_scale_ft / floorplan distinguish "absent" (sticky)
    # from explicit null (clear).
    for nullable_key in ("secure_area", "map_scale_ft", "floorplan"):
        if nullable_key not in body:
            merged[nullable_key] = policy_settings.get_active().get(nullable_key)
    new_rev = policy_settings.save_settings(settings.push.push_settings_path, merged)
    policy_settings.apply_settings(merged)
    return {"ok": True, "rev": new_rev}


_ERR_CONFIG_REFRESH = "config_refresh_failed"


@router.post("/frigate-config/refresh")
async def refresh_frigate_config(request: Request) -> dict[str, Any]:
    """Re-sync the sidecar's Frigate-config copy from Frigate itself.

    A deployment whose `frigate.config_path` points at the live config file
    (prod) never needs this — camera/zone reads go to that file per request.
    A dev instance reads a *snapshot*, which goes stale the moment cameras
    or zones are renamed in Frigate; this fetches `/api/config/raw` from the
    authenticated origin (with the requester's own session cookie, so it
    grants nothing the caller doesn't already have) and rewrites the
    snapshot when it changed."""
    import httpx

    from frigate_sidecar.frigate_api import get_async_client

    settings = request.app.state.settings
    if not settings.frigate.config_refresh_enabled:
        # On prod `frigate.config_path` is Frigate's live config.yml -- an
        # overwrite here would clobber it. Only a deployment that has declared
        # its copy to be a sidecar-owned snapshot may refresh it.
        raise HTTPException(
            status_code=403,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": "frigate.config_refresh_enabled is off -- refusing to "
                "overwrite frigate.config_path",
            },
        )
    upstream = settings.frigate.proxy_base_url.rstrip("/") + "/api/config/raw"
    client = get_async_client(request.app)
    try:
        resp = await client.get(
            upstream,
            headers={"cookie": request.headers.get("cookie", "")},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail={"error": _ERR_CONFIG_REFRESH, "message": str(exc)},
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": f"frigate answered HTTP {resp.status_code}",
            },
        )
    raw = resp.text
    try:
        import yaml

        parsed = yaml.safe_load(raw)
    except Exception:
        parsed = None
    if not (isinstance(parsed, dict) and isinstance(parsed.get("cameras"), dict)):
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": "response did not look like a Frigate config",
            },
        )

    path = pathlib.Path(settings.frigate.config_path)

    def _rewrite_snapshot() -> bool:
        current = path.read_text() if path.exists() else None
        if raw == current:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            # Keep the outgoing content: this endpoint is the only writer that
            # can destroy a config it didn't author.
            path.with_suffix(path.suffix + ".bak").write_text(current)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(raw)
        tmp.replace(path)
        return True

    changed = await asyncio.to_thread(_rewrite_snapshot)
    if changed:
        policy_settings.load_zone_display_names(path)

    from frigate_sidecar.zones import load_camera_zones

    return {"changed": changed, "cameras": sorted(load_camera_zones(path).keys())}


