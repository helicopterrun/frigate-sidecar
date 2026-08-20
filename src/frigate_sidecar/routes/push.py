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

import asyncio
import logging
import pathlib
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from frigate_sidecar import db
from frigate_sidecar.push import decision_trace, library, policy_settings, store
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
    la_capable: bool = True
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

    def _persist(conn: Any) -> tuple[Any, Any, Any]:
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
            la_capable=body.la_capable,
        )
        if body.snoozes is not None:
            store.replace_snoozes(conn, apns_token=apns_token, snoozes=body.snoozes)
        conn.commit()
        # Read back rather than echoing the request: a PUT that omits
        # `push_to_start_token` keeps the one already stored, so the body alone
        # can't answer "can this device run Live Activities".
        return previous, device_id, store.get_device(conn, apns_token)

    previous, device_id, stored = await db.with_sidecar(settings.sidecar.db_path, _persist)

    # Recorded for back-compat visibility only: the situations pipeline is
    # retired (Phase 5 §1, card pipeline is the sole alert path), but which
    # mode a device *registered* under still matters for tracing older app
    # builds, so the row keeps its situations and this line keeps logging.
    uses_situations = bool(parsed)
    logger.info(
        "push: registration apns_token=%s schema_version=%s uses_situations=%s "
        "(situations stored for back-compat; card pipeline is the alert path)",
        apns_token[:8], schema_version, uses_situations,
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
        "la_capable": bool(stored.la_capable) if stored else True,
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

    def _delete(conn: Any) -> None:
        store.delete_device(conn, apns_token)
        conn.commit()

    await db.with_sidecar(settings.sidecar.db_path, _delete)
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
    device = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_device(conn, apns_token)
    )
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

    def _snooze(conn: Any) -> Any:
        if store.get_device(conn, body.apns_token) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
            )
        store.set_snooze(
            conn, apns_token=body.apns_token, scope=scope, until_epoch=body.until_epoch
        )
        conn.commit()
        return store.list_snoozes(conn, body.apns_token)

    active = await db.with_sidecar(settings.sidecar.db_path, _snooze)
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

    def _unsnooze(conn: Any) -> Any:
        store.clear_snooze(conn, apns_token=apns_token, scope=scope)
        conn.commit()
        return store.list_snoozes(conn, apns_token)

    active = await db.with_sidecar(settings.sidecar.db_path, _unsnooze)
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
    device = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_device(conn, body.apns_token)
    )
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

    def _attach(conn: Any) -> Any:
        device = store.get_device(conn, body.apns_token)
        if device is None:
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
        return device

    device = await db.with_sidecar(settings.sidecar.db_path, _attach)

    # Fast create→resolve race: the card may already be closed by the time
    # this token arrives. End the activity now rather than leaving it
    # stranded on the lock screen until its stale-date. This part can't ride
    # in `_attach`'s worker thread: `end_activity_if_card_closed` awaits the
    # transport while holding the connection (sqlite connections are
    # thread-bound), so this rare path keeps a loop-thread connection.
    engine = getattr(request.app.state, "push_engine", None)
    if engine is not None:
        from frigate_sidecar.push.delivery_wire import end_activity_if_card_closed

        conn = db.open_sidecar(settings.sidecar.db_path)
        try:
            await end_activity_if_card_closed(
                conn, device, engine.transport,
                card_key=body.situation_id, track_id=body.track_id, token=body.token,
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

    def _delete(conn: Any) -> Any:
        removed = store.delete_activity(conn, activity_id)
        conn.commit()
        return removed

    removed = await db.with_sidecar(settings.sidecar.db_path, _delete)
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
    jpeg = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_thumbnail(conn, handle)
    )
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
    data = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.redeem_handle(conn, handle)
    )
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


@router.get("/decisions")
async def get_decisions(limit: int = Query(default=50, ge=1)) -> dict[str, Any]:
    """Recent routing decisions, newest first (spec §7)."""
    return {"decisions": decision_trace.recent(limit)}


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


_ERR_BAD_FLOORPLAN = "bad_floorplan"
_ERR_FLOORPLAN_NOT_FOUND = "floorplan_not_found"
_FLOORPLAN_MAX_BYTES = 10 * 1024 * 1024
_FLOORPLAN_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """(ext, width, height) from the image's own header bytes — the upload
    is trusted on its magic bytes, never its Content-Type. PNG/JPEG/WebP
    only; a tiny hand parse so Pillow doesn't become a dependency for one
    dimensions read."""
    # byteorder is explicit everywhere: the "big" default only exists on
    # Python 3.11+, and prod runs 3.10 (an omission here 500'd every PNG).
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return (
            "png",
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    "jpg",
                    int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"),
                )
            i += 2 + length
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        fmt = data[12:16]
        if fmt == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return ("webp", w, h)
        if fmt == b"VP8 ":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return ("webp", w, h)
        if fmt == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return ("webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _floorplan_file(request: Request) -> pathlib.Path | None:
    active = policy_settings.get_active()
    fp = active.get("floorplan")
    if not isinstance(fp, dict):
        return None
    base = pathlib.Path(request.app.state.settings.push.floorplan_path)
    path = base.with_suffix("." + fp["ext"])
    return path if path.exists() else None


@router.post("/floorplan")
async def upload_floorplan(request: Request) -> dict[str, Any]:
    """Store the layout map's background image. Raw image bytes as the body
    (no multipart -- the page POSTs the File object directly), identified by
    magic bytes. Replaces any previous floorplan and resets the scale
    calibration line -- a new image means the old line's coordinates are
    meaningless."""
    data = await request.body()
    if len(data) > _FLOORPLAN_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": "image exceeds 10 MB"},
        )
    sniffed = _sniff_image(data)
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": "not a PNG, JPEG, or WebP image"},
        )
    ext, w, h = sniffed
    if not (0 < w <= 20000 and 0 < h <= 20000):
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": f"unreasonable dimensions {w}x{h}"},
        )

    settings = request.app.state.settings
    base = pathlib.Path(settings.push.floorplan_path)

    def _store_image() -> None:
        base.parent.mkdir(parents=True, exist_ok=True)
        tmp = base.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(base.with_suffix("." + ext))
        for other in _FLOORPLAN_MEDIA_TYPES:
            if other != ext:
                base.with_suffix("." + other).unlink(missing_ok=True)

    await asyncio.to_thread(_store_image)

    from datetime import datetime, timezone

    fp = {
        "ext": ext, "w": w, "h": h,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calibration": None,
    }
    active = dict(policy_settings.get_active())
    active["floorplan"] = fp
    policy_settings.save_settings(settings.push.push_settings_path, active)
    policy_settings.apply_settings(active)
    return {"floorplan": fp}


@router.get("/floorplan")
async def get_floorplan(request: Request) -> Response:
    path = _floorplan_file(request)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_FLOORPLAN_NOT_FOUND, "message": "no floorplan uploaded"},
        )
    return Response(
        content=await asyncio.to_thread(path.read_bytes),
        media_type=_FLOORPLAN_MEDIA_TYPES[path.suffix.lstrip(".")],
        headers={"Cache-Control": "no-cache"},
    )


@router.delete("/floorplan")
async def delete_floorplan(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    base = pathlib.Path(settings.push.floorplan_path)
    for ext in _FLOORPLAN_MEDIA_TYPES:
        base.with_suffix("." + ext).unlink(missing_ok=True)
    active = dict(policy_settings.get_active())
    active["floorplan"] = None
    policy_settings.save_settings(settings.push.push_settings_path, active)
    policy_settings.apply_settings(active)
    return {"ok": True}


@router.get("/map/zones")
async def map_zones(request: Request) -> dict[str, Any]:
    """Frigate zone polygons projected onto the floorplan map.

    One entry per (camera, zone): a zone watched by several cameras yields
    several overlapping polygons — overlap is honest coverage evidence, so
    the UI draws all of them translucently rather than merging. Cameras
    without layout/optics/scale and full-frame gate zones are omitted;
    `clipped` marks polygons cut at the horizon/range limit (some source
    vertices didn't project).
    """
    import time as _time

    from frigate_sidecar.push import fusion, ground
    from frigate_sidecar.zones import is_full_frame, load_camera_zones

    settings = request.app.state.settings
    active = policy_settings.get_active()
    layout_table = active.get("camera_layout") or {}
    scale_ft = active.get("map_scale_ft")
    aspect = ground.map_aspect(active)
    zones: list[dict[str, Any]] = []
    if scale_ft and scale_ft > 0:
        for camera, zone_list in load_camera_zones(settings.frigate.config_path).items():
            layout = layout_table.get(camera)
            if not layout:
                continue
            for zone in zone_list:
                if is_full_frame(zone["coords"]):
                    continue
                pts = fusion.project_polygon(
                    zone["coords"], camera=camera, layout_entry=layout,
                    scale_ft=scale_ft, aspect_h_over_w=aspect,
                )
                if pts is None:
                    continue
                zones.append({
                    "camera": camera,
                    "name": zone["name"],
                    "color": zone["color"],
                    "objects": zone["objects"],
                    "points": [[round(x, 4), round(y, 4)] for x, y in pts],
                    "clipped": len(pts) != len(zone["coords"]),
                })
    return {"t": _time.time(), "aspect": aspect, "zones": zones}


@router.get("/map/footprints")
async def map_footprints(request: Request) -> dict[str, Any]:
    """Each placed camera's true projected ground footprint: the full
    image frame pushed through its optics onto the floorplan, densified
    and clipped at the horizon/range limit — the honest coverage view.
    Cameras without layout/optics/scale are omitted."""
    import time as _time

    from frigate_sidecar.push import fusion, ground

    active = policy_settings.get_active()
    layout_table = active.get("camera_layout") or {}
    scale_ft = active.get("map_scale_ft")
    aspect = ground.map_aspect(active)
    frame = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    footprints: list[dict[str, Any]] = []
    if scale_ft and scale_ft > 0:
        for camera in sorted(layout_table):
            layout = layout_table[camera]
            pts = fusion.project_polygon(
                frame, camera=camera, layout_entry=layout,
                scale_ft=scale_ft, aspect_h_over_w=aspect,
            )
            if pts is None:
                continue
            footprints.append({
                "camera": camera,
                "points": [[round(x, 4), round(y, 4)] for x, y in pts],
                "clipped": len(pts) != len(frame),
            })
    return {"t": _time.time(), "aspect": aspect, "footprints": footprints}


@router.get("/map/live")
async def map_live(request: Request, debug: int = Query(default=0)) -> dict[str, Any]:
    """Current fused object positions on the floorplan map, for the /cameras
    Live overlay (polled ~1 Hz). Cross-camera sightings of the same object
    merge into one entry listing every contributing camera."""
    import time as _time

    from frigate_sidecar.push import fusion, ground

    engine = getattr(request.app.state, "push_engine", None)
    active = policy_settings.get_active()
    scale_ft = active.get("map_scale_ft")
    now = _time.time()
    objects: list[dict[str, Any]] = []
    if engine is not None and scale_ft and scale_ft > 0:
        positions = fusion.track_world_positions(engine.tracks, active, now=now)
        aspect = ground.map_aspect(active)
        for c in fusion.cluster(positions, scale_ft=scale_ft, aspect_h_over_w=aspect):
            entry: dict[str, Any] = {
                "x": round(c.x, 4),
                "y": round(c.y, 4),
                "label": c.label,
                "stationary": c.stationary,
                "cameras": [m.camera for m in c.members],
                "track_ids": [m.track_id for m in c.members],
            }
            if debug:
                entry["members"] = [
                    {
                        "camera": m.camera, "track_id": m.track_id,
                        "x": round(m.x, 4), "y": round(m.y, 4),
                        "forward_ft": round(m.forward_ft, 1),
                        "age_s": round(m.age_s, 2),
                    }
                    for m in c.members
                ]
            objects.append(entry)
    return {"t": now, "objects": objects}


@router.get("/map/track")
async def map_track(
    request: Request, camera: str = Query(...), event_id: str = Query(...),
) -> dict[str, Any]:
    """One event's trail projected onto the floorplan map, for the app's
    event mini-map. Live tracks come from the engine's track store; ended
    events fall back to the MQTT flight recorder (same source as
    /replay/capture-window). 404 `not_projectable` whenever the world model
    can't answer — the app renders nothing rather than a guess."""
    import time as _time

    from frigate_sidecar.push import ground
    from frigate_sidecar.routes.replay import _capture_paths, _capture_tracks

    active = policy_settings.get_active()
    scale_ft = active.get("map_scale_ft")
    layout = (active.get("camera_layout") or {}).get(camera)
    if (
        not scale_ft or scale_ft <= 0 or not layout
        or layout.get("azimuth") is None or ground.camera_ground(camera) is None
    ):
        raise HTTPException(status_code=404, detail="not_projectable")

    path_data: list | None = None
    engine = getattr(request.app.state, "push_engine", None)
    if engine is not None:
        state = engine.tracks.get(camera, event_id)
        if state is not None and state.path_data:
            path_data = list(state.path_data)
    if path_data is None:
        # Flight-recorder fallback: last 24 h, this camera only.
        rows = _capture_tracks(
            _capture_paths(request.app.state.settings),
            _time.time() - 24 * 3600.0, camera=camera,
        )
        for row in rows:
            if row["track_id"] == event_id:
                path_data = row["points"]
                break
    if not path_data:
        raise HTTPException(status_code=404, detail="not_projectable")

    aspect = ground.map_aspect(active)
    projected: list[list[float]] = []
    for pt in path_data:
        wp = ground.world_position(
            pt[0], pt[1], camera=camera, layout_entry=layout,
            scale_ft=scale_ft, aspect_h_over_w=aspect,
        )
        if wp is not None:
            projected.append([round(wp[0], 4), round(wp[1], 4)])
    if len(projected) < 2:
        raise HTTPException(status_code=404, detail="not_projectable")

    if len(projected) > 60:  # even decimation, endpoints preserved
        stride = (len(projected) - 1) / 59
        projected = [projected[round(k * stride)] for k in range(60)]

    secure = active.get("secure_area")
    distances = [
        d for p in projected
        if (d := ground.distance_to_secure_ft(
            p[0], p[1], secure, scale_ft=scale_ft, aspect_h_over_w=aspect,
        )) is not None
    ]
    speed = ground.speed_ft_s(path_data, camera)
    return {
        "points_map": projected,
        "camera": {"x": layout.get("x", 0.0), "y": layout.get("y", 0.0)},
        "secure_area": secure if isinstance(secure, dict) else None,
        "aspect": round(aspect, 4),
        "speed_ft_s": round(speed, 1) if speed is not None else None,
        "distance_ft_range": (
            [round(min(distances), 1), round(max(distances), 1)] if distances else None
        ),
    }


@router.post("/map/landmark-solve")
async def map_landmark_solve(request: Request) -> dict[str, Any]:
    """Solve one camera's HFOV/azimuth/tilt from landmark matches.

    Body: `{"camera": str, "matches": [{"u","v","mx","my"}, ...]}` — each
    match pairs a click in the camera frame with the same physical spot
    clicked on the calibrated floorplan. Pure preview: returns the solved
    values + per-match residuals; the /cameras page applies accepted
    numbers through the normal Save flow.
    """
    from frigate_sidecar.push import calibrate

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    camera = body.get("camera")
    matches = body.get("matches")
    if not isinstance(camera, str) or not isinstance(matches, list):
        raise HTTPException(status_code=400, detail="camera and matches required")
    if not (2 <= len(matches) <= 12):
        raise HTTPException(status_code=400, detail="need 2-12 landmark matches")
    clean = []
    for m in matches:
        try:
            entry = {k: float(m[k]) for k in ("u", "v", "mx", "my")}
        except (TypeError, KeyError, ValueError):
            raise HTTPException(
                status_code=400, detail="each match needs numeric u, v, mx, my",
            ) from None
        if not all(-0.5 <= v <= 1.5 for v in entry.values()):
            raise HTTPException(status_code=400, detail="match coords out of range")
        clean.append(entry)
    try:
        return calibrate.solve_landmarks(camera, clean, policy_settings.get_active())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/feedback")
async def post_feedback(request: Request) -> dict[str, Any]:
    """Log user feedback on a push notification (tuning trace, no routing
    changes this phase). Accepts any verdict string for forward compat."""
    body = await request.json()
    if not isinstance(body, dict) or "card_key" not in body or "verdict" not in body:
        raise HTTPException(status_code=400, detail="card_key and verdict required")
    logger.info(
        "push-feedback: card_key=%s event_id=%s verdict=%s",
        body["card_key"], body.get("event_id", ""), body["verdict"],
    )
    return {"ok": True}
