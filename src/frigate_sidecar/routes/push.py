"""`/v1/push` -- device registration/unregistration and handle redemption.

Auth is the shared Frigate session (`frigate_sidecar.auth`), same as every
other sidecar-owned route -- there is no second credential for push (spec
§1: "the sidecar just attaches the device token to that session's
identity"). The one exception is `GET /v1/push/thumbnail/{handle}`
(`auth.EXEMPT_PREFIXES`): the iOS Notification Service Extension fetches it
and holds no Frigate session, so it's protected by the handle itself being
opaque, unguessable, and short-lived instead.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from frigate_sidecar import db
from frigate_sidecar.push import library, store
from frigate_sidecar.push.situations import Situation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_HANDLE_NOT_FOUND = "handle_not_found"
_ERR_DEVICE_NOT_FOUND = "device_not_found"
_ERR_PUSH_DISABLED = "push_disabled"
_ERR_TEST_SEND_FAILED = "test_send_failed"
_ERR_SITUATION_NOT_FOUND = "situation_not_found"
_ERR_THUMBNAIL_NOT_FOUND = "thumbnail_not_found"
_ERR_BAD_SCOPE = "bad_scope"


class DeviceLocation(BaseModel):
    lat: float
    lon: float


class DeviceRegistration(BaseModel):
    """The v2 registration record (notification-experience plan §8).

    Every v1 field keeps its meaning and its default, so a phone running an
    older app build PUTs exactly what it PUT before and is stored exactly as
    it was stored before. Everything added below is optional; an omitted
    `situations` is what keeps that device on the v1 firing path.

    Unknown fields are ignored rather than rejected -- the app ships on its
    own cadence and a newer build must not 422 against an older sidecar. They
    are *logged* though (names only, never values): silently dropping a field
    the app believes it sent is how `push_to_start_token` spent a day looking
    like an app-side bug when the sidecar simply hadn't learned the name yet.
    """

    model_config = ConfigDict(extra="allow")

    bundle_id: str
    # Not optional, not inferred (spec §1) -- sandbox and production APNs are
    # different endpoints with different trust; the app must read its own
    # `aps-environment` entitlement and say so.
    environment: Literal["sandbox", "prod"]
    app_version: str = ""
    cameras: list[str] = Field(default_factory=list)  # [] = all cameras
    labels: list[str] = Field(default_factory=list)  # [] = all labels
    min_severity: Literal["alert", "detection"] = "alert"

    # -- v2 --
    schema_version: int = 1
    timezone: str = ""  # IANA name, e.g. "America/Los_Angeles"
    location: DeviceLocation | None = None
    situations: list[dict[str, Any]] = Field(default_factory=list)
    # None means "the client didn't mention snoozes", which must leave the
    # ones it set earlier alone -- see `store.replace_snoozes`. An explicit
    # [] is a request to clear them.
    snoozes: list[dict[str, Any]] | None = None

    # -- Phase 2 --
    # One per app install, rotates on reinstall; creates Live Activities.
    # Absent means this device isn't ready for them, and its Present-tier
    # situations fall back to alert pushes.
    push_to_start_token: str = ""

    # Accepted, persisted, and deliberately unread (Phase 4's digest and LLM).
    live_activity_token: str = ""
    morning_digest: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None


class SnoozeRequest(BaseModel):
    """`POST /v1/push/snooze` (plan §8).

    `apns_token` identifies the device, because nothing else can: the sidecar's
    auth is the shared Frigate session, which is per-*user*, and snoozes are
    per-*device* by design (plan §6 -- snoozing on the iPad must not quiet the
    iPhone).
    """

    apns_token: str
    scope: str  # "situation:<id>" | "camera:<name>" | "global"
    until_epoch: float


class SituationTestRequest(BaseModel):
    apns_token: str


class ActivityTokenUpload(BaseModel):
    """`POST /v1/push/activity/token` (Phase 2 plan, "Push tokens").

    Carries both identities on purpose: `activity_id` is what the app knows
    and what the delete endpoint addresses, while
    `(apns_token, situation_id, track_id)` is what the MQTT stream can look an
    activity up by when it has an update to send.
    """

    apns_token: str
    situation_id: str
    track_id: str
    activity_id: str
    token: str


def _validate_scope(scope: str) -> str:
    scope = scope.strip()
    if scope == "global":
        return scope
    # A prefix with nothing after it ("situation:") would silence nothing
    # while looking exactly like a snooze that took.
    if scope.startswith(("situation:", "camera:")) and scope.split(":", 1)[1]:
        return scope
    raise HTTPException(
        status_code=422,
        detail={
            "error": _ERR_BAD_SCOPE,
            "message": "scope must be 'global', 'situation:<id>', or 'camera:<name>'",
        },
    )


@router.put("/devices/{apns_token}")
async def register_device(
    apns_token: Annotated[str, Path(min_length=1)],
    body: DeviceRegistration,
    request: Request,
) -> dict[str, Any]:
    """Idempotent PUT on the token (spec §1) -- re-registering (relaunch,
    entitlement refresh) overwrites this device's own filter state rather
    than accumulating duplicate rows that would double-fire alerts.

    The response echoes how the sidecar will actually evaluate this device:
    `schema_version: 1` (today's camera+label+severity firing) or `2`
    (situation-only), plus how many of the submitted situations parsed. A
    situation the sidecar silently discarded -- no `id`, say -- would
    otherwise look enabled in the app and never fire.
    """
    settings = request.app.state.settings
    extras = sorted(body.model_extra or ())
    if extras:
        logger.info(
            "push: registration for %s carried field(s) this sidecar does not "
            "know: %s -- accepted and dropped",
            store.device_id_for_token(apns_token), ", ".join(extras),
        )
    situations = [s for s in body.situations if isinstance(s, dict)]
    parsed = [s for s in (Situation.from_dict(s) for s in situations) if s is not None]
    schema_version = 2 if parsed else body.schema_version

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        previous = store.get_device(conn, apns_token)
        device_id = store.upsert_device(
            conn,
            apns_token=apns_token,
            bundle_id=body.bundle_id,
            environment=body.environment,
            app_version=body.app_version,
            cameras=body.cameras,
            labels=body.labels,
            min_severity=body.min_severity,
            schema_version=schema_version,
            timezone_name=body.timezone,
            location=body.location.model_dump() if body.location else None,
            situations=situations,
            live_activity_token=body.live_activity_token,
            morning_digest=body.morning_digest,
            llm=body.llm,
            push_to_start_token=body.push_to_start_token,
        )
        if body.snoozes is not None:
            store.replace_snoozes(conn, apns_token=apns_token, snoozes=body.snoozes)
        conn.commit()
        # Read back rather than echoing the request: a PUT that omits
        # `push_to_start_token` keeps the one already stored, so the body alone
        # can't answer "can this device run Live Activities".
        stored = store.get_device(conn, apns_token)
    finally:
        conn.close()

    # Which mode this device evaluates under is otherwise invisible short of
    # dumping the row: an empty `situations` array is a legitimate, deliberate
    # choice (v1 camera/label/severity dispatch), but nothing recorded *that*
    # a device landed there until this line existed.
    uses_situations = bool(parsed)
    dispatch = "situation matching (_dispatch_situations)" if uses_situations else "v1 camera/label/severity (_dispatch_v1)"
    logger.info(
        "push: registration apns_token=%s schema_version=%s uses_situations=%s dispatch=%s",
        apns_token[:8], schema_version, uses_situations, dispatch,
    )
    if previous is not None and previous.uses_situations != uses_situations:
        # The edge that actually matters: a device silently flipping mode
        # (e.g. an app reinstall wiping stored situations) looks identical to
        # a healthy re-registration from the outside. This is the line that
        # would have caught it in seconds instead of a four-hour trace.
        logger.info(
            "push: registration apns_token=%s transitioned uses_situations %s -> %s",
            apns_token[:8], previous.uses_situations, uses_situations,
        )

    # Echo back what the sidecar will actually do with this device, including
    # whether Live Activities are available to it -- the app-side token flow is
    # asynchronous, so "did my push-to-start token land" is a real question
    # with no other way to answer it.
    return {
        "registered": True,
        "device_id": device_id,
        "schema_version": schema_version,
        "situations_accepted": len(parsed),
        "live_activities": bool(stored and stored.can_live_activity),
    }


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


@router.get("/situations/library")
async def situations_library() -> list[dict[str, Any]]:
    """The starter situations a new user can enable with one tap (plan §1).

    Cameras and zones are placeholders using the plan's own example names --
    the app's editor replaces them with real ones read from the user's Frigate
    `/api/config` before registering. A starter left unedited matches only if
    the user happens to have a zone by that name, which fails silent rather
    than firehose.
    """
    return library.starter_library()


@router.get("/sounds")
async def sounds(app_version: str = Query("")) -> list[dict[str, str]]:
    """The sound ids a situation's `sound` field may name (plan §3).

    The `.caf` assets ship in the *app* bundle, not here, so the catalog is
    keyed on `app_version`: advertising a sound an older build doesn't contain
    would deliver a silent notification, which reads as broken at exactly the
    wrong moment. Phase 1 ships one catalog for every version.
    """
    return library.sound_catalog(app_version)


@router.post("/snooze")
async def create_snooze(body: SnoozeRequest, request: Request) -> dict[str, Any]:
    """Silence one scope for one device until `until_epoch` (plan §6).

    Sidecar-side because it has to survive an app kill, and because the
    interactive widget's "Snooze all 15m" must reach the source of truth
    without the app running at all. Expiry is a timestamp, not a scheduled
    job: it re-enables itself with nothing to run and nothing to miss if the
    sidecar was restarted in the meantime.

    Deprecated (sidecar-snooze-and-v2-investigation handoff, Thread B):
    superseded by `registration.snoozes`, a full-state replace on every
    `PUT /v1/push/devices/{token}` -- point-updates writing the same store a
    whole-state sync writes let local and sidecar snooze state drift apart
    invisibly. Kept for one release so an app build that still calls this
    keeps working; slated for removal once that build has aged out.
    """
    logger.warning(
        "push: deprecated POST /v1/push/snooze called for device %s -- "
        "use registration.snoozes instead", body.apns_token,
    )
    scope = _validate_scope(body.scope)
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        if store.get_device(conn, body.apns_token) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
            )
        store.set_snooze(
            conn, apns_token=body.apns_token, scope=scope, until_epoch=body.until_epoch
        )
        conn.commit()
        active = store.list_snoozes(conn, body.apns_token)
    finally:
        conn.close()
    return {"snoozed": True, "scope": scope, "until_epoch": body.until_epoch, "active": active}


@router.delete("/snooze/{scope:path}")
async def delete_snooze(
    scope: str, request: Request, apns_token: str = Query(..., min_length=1)
) -> dict[str, Any]:
    """Lift a snooze early. Idempotent: clearing one that already expired (or
    never existed) is still a 200, since the end state either way is "not
    snoozed".

    `{scope:path}` because a scope contains a colon (`situation:at-the-door`)
    and, for `camera:<name>`, whatever the user named their camera.

    Deprecated (sidecar-snooze-and-v2-investigation handoff, Thread B): same
    reasoning as `POST /v1/push/snooze` -- lifting a snooze is just
    re-registering with a shorter (or absent) `snoozes` array now.
    """
    logger.warning(
        "push: deprecated DELETE /v1/push/snooze/%s called for device %s -- "
        "use registration.snoozes instead", scope, apns_token,
    )
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        store.clear_snooze(conn, apns_token=apns_token, scope=scope)
        conn.commit()
        active = store.list_snoozes(conn, apns_token)
    finally:
        conn.close()
    return {"unsnoozed": True, "scope": scope, "active": active}


@router.post("/test/{situation_id}")
async def test_situation_push(
    situation_id: Annotated[str, Path(min_length=1)],
    body: SituationTestRequest,
    request: Request,
) -> dict[str, Any]:
    """Fire one push for `situation_id` at the calling device (plan §8).

    The device is named in the body rather than inferred from the session:
    the sidecar's auth is the shared Frigate session, which identifies a
    *user*, and this endpoint is per-*device* -- there is nothing on the
    request that could tell two of a user's phones apart.

    Runs the whole real path (handle, thumbnail pre-warm, payload, collapse
    id, sound), because what the app's Settings button is verifying is that a
    real situation push arrives looking the way it should. Snooze and the
    rate-limit ceiling are bypassed and the send isn't charged against the
    hourly budget -- the user asked for this one.
    """
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        device = store.get_device(conn, body.apns_token)
    finally:
        conn.close()
    if device is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
        )

    situation = next((s for s in device.situations if s.id == situation_id), None)
    if situation is None:
        # Falling back to the starter library would test a rule the device
        # isn't actually registered with, which is the one thing this button
        # must not quietly do.
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_SITUATION_NOT_FOUND,
                "message": f"device has no situation {situation_id!r}",
            },
        )

    engine = getattr(request.app.state, "push_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _ERR_PUSH_DISABLED,
                "message": "push is not enabled on this server (push.enabled=false)",
            },
        )

    result = await engine.send_situation_test(device, situation)
    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_TEST_SEND_FAILED,
                "message": result.error or "push transport rejected the send",
            },
        )
    return {"sent": True, "situation_id": situation_id}


@router.post("/activity/token")
async def upload_activity_token(
    body: ActivityTokenUpload, request: Request
) -> dict[str, Any]:
    """The app hands over a Live Activity's own push token (Phase 2).

    iOS mints this token *after* creating the activity from the start push, so
    there is always a window where an activity is on screen that the sidecar
    cannot yet update. That is normal: updates resume on the next observation
    once this lands.

    Keyed on `activity_id` (what the app knows) and looked up on
    `(apns_token, situation_id, track_id)` (what the MQTT stream knows) --
    which is why the body carries both.
    """
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        if store.get_device(conn, body.apns_token) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
            )
        store.attach_activity_token(
            conn,
            activity_id=body.activity_id,
            apns_token=body.apns_token,
            situation_id=body.situation_id,
            track_id=body.track_id,
            token=body.token,
        )
        conn.commit()
    finally:
        conn.close()
    return {"accepted": True, "activity_id": body.activity_id}


@router.delete("/activity/token/{activity_id}")
async def delete_activity_token(
    activity_id: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """The app ended the activity locally (user swiped it away, or its own
    lifecycle finished).

    Drops the row outright rather than marking it ended: there is nothing left
    to send an end push to, and leaving a tokened row behind would have the
    sweeper try. Idempotent -- an unknown id is still a 200, since the end
    state either way is "the sidecar isn't tracking that activity".
    """
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        removed = store.delete_activity(conn, activity_id)
        conn.commit()
    finally:
        conn.close()
    return {"deleted": True, "activity_id": activity_id, "was_tracked": removed}


@router.get("/thumbnail/{handle}")
async def get_thumbnail(handle: str, request: Request) -> Response:
    """The NSE's pre-warmed snapshot fetch (plan §8).

    Same auth as every other `/v1/` endpoint -- the extension reads the app's
    session credential from the shared Keychain access group, so there is no
    second credential and no second login flow (transport spec §3).

    A miss is a 404 and the app delivers the alert without an image: the
    visible push is the promise, the image is not.
    """
    settings = request.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        jpeg = store.get_thumbnail(conn, handle)
    finally:
        conn.close()
    if jpeg is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_THUMBNAIL_NOT_FOUND,
                "message": "no thumbnail for that handle (expired, unknown, or never warmed)",
            },
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        # Immutable for the handle's life: a handle is minted per push and
        # never reused, so the bytes behind one can't change.
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
