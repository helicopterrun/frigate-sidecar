"""`/v1/push` -- device registration/unregistration and handle redemption.

Auth is the shared Frigate session (`frigate_sidecar.auth`), same as every
other sidecar-owned route -- there is no second credential for push (spec
§1: "the sidecar just attaches the device token to that session's
identity"). Nothing here is exempted in `auth.EXEMPT_PATHS`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from frigate_sidecar import db
from frigate_sidecar.push import store

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_HANDLE_NOT_FOUND = "handle_not_found"
_ERR_DEVICE_NOT_FOUND = "device_not_found"
_ERR_PUSH_DISABLED = "push_disabled"
_ERR_TEST_SEND_FAILED = "test_send_failed"


class DeviceRegistration(BaseModel):
    bundle_id: str
    # Not optional, not inferred (spec §1) -- sandbox and production APNs are
    # different endpoints with different trust; the app must read its own
    # `aps-environment` entitlement and say so.
    environment: Literal["sandbox", "prod"]
    app_version: str = ""
    cameras: list[str] = Field(default_factory=list)  # [] = all cameras
    labels: list[str] = Field(default_factory=list)  # [] = all labels
    min_severity: Literal["alert", "detection"] = "alert"


@router.put("/devices/{apns_token}")
async def register_device(
    apns_token: Annotated[str, Path(min_length=1)],
    body: DeviceRegistration,
    request: Request,
) -> dict[str, Any]:
    """Idempotent PUT on the token (spec §1) -- re-registering (relaunch,
    entitlement refresh) overwrites this device's own filter state rather
    than accumulating duplicate rows that would double-fire alerts."""
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        device_id = store.upsert_device(
            conn,
            apns_token=apns_token,
            bundle_id=body.bundle_id,
            environment=body.environment,
            app_version=body.app_version,
            cameras=body.cameras,
            labels=body.labels,
            min_severity=body.min_severity,
        )
        conn.commit()
    finally:
        conn.close()
    return {"registered": True, "device_id": device_id}


@router.delete("/devices/{apns_token}")
async def unregister_device(
    apns_token: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """Explicit unregistration (notification-toggle-off, or best-effort after
    registration failure). Not the primary cleanup path -- that's the 410/400
    feedback loop in `push.engine` (spec §5) -- because the app can't promise
    to run this before uninstall. Idempotent: unregistering an unknown token
    is still a 200, not a 404, since the end state either way is "not
    registered"."""
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        store.delete_device(conn, apns_token)
        conn.commit()
    finally:
        conn.close()
    return {"unregistered": True}


@router.post("/devices/{apns_token}/test")
async def test_push(
    apns_token: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """Send one test push to exactly this device (spec §1, "Test push").

    `{"sent": true}` means APNs *accepted* the request -- there is no delivery
    receipt, so it can never mean "displayed on the device".

    404 is reserved for "token not registered": the released iOS client maps it
    to "your server doesn't support test notifications yet", so no other
    condition may borrow it. A rejected send is 502 and a server with push
    switched off is 503, both carrying the standard error envelope.
    """
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        device = store.get_device(conn, apns_token)
    finally:
        conn.close()
    if device is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
        )

    # Registration writes to the DB whether or not push is enabled, so a
    # registered token can exist on a server with no engine running. Say so
    # rather than reporting a send that no transport ever attempted.
    engine = getattr(request.app.state, "push_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _ERR_PUSH_DISABLED,
                "message": "push is not enabled on this server (push.enabled=false)",
            },
        )

    result = await engine.send_test(device)
    if not result.ok:
        # Includes the 410/400 case, where the engine has already deleted the
        # row per spec §5 -- the device is gone and the client should re-register.
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_TEST_SEND_FAILED,
                "message": result.error or "push transport rejected the send",
            },
        )
    return {"sent": True}


@router.get("/handle/{handle}")
async def redeem_handle(handle: str, request: Request) -> dict[str, Any]:
    """NSE / app handle redemption (spec §3 step 2). Returns the camera and
    Frigate event id a handle stands for, plus the `snapshot_url` to fetch
    next -- never the raw event id in the APNs payload itself."""
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        data = store.redeem_handle(conn, handle)
    finally:
        conn.close()
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_HANDLE_NOT_FOUND, "message": "handle not found or expired"},
        )
    return {
        "camera": data["camera"],
        "event_id": data["event_id"],
        "snapshot_url": f"/api/events/{data['event_id']}/snapshot.jpg",
    }
