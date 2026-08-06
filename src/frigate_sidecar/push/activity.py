"""Live Activity payloads (Phase 2 plan, "Push contract additions").

Three shapes, all `apns-push-type: liveactivity`, all carrying `aps.event`:

* **start** -- goes to the device's *push-to-start* token and asks iOS to
  create the activity. The only one carrying `attributes` (the static side)
  and an `alert` body.
* **update** -- goes to the *per-activity* token iOS handed the app after the
  start. Silent by construction: no `alert` key, so nothing buzzes.
* **end** -- same token, plus a `dismissal-date` so the activity fades on its
  own tail rather than vanishing the instant the object leaves.

`ContentState` is a wire contract shared with the app's `Codable` struct, so
the key names here are load-bearing: `stage`, `dwell_seconds`, `title`,
`subtitle`, `thumbnail_revision`, snake_case exactly as the handoff spells
them.

The static attributes travel only on the start push -- `situation_id`,
`situation_name`, `camera_id`, `handle`. The handle is static because the
widget fetches its thumbnail from the same `pushThumbnail` endpoint the NSE
uses, and a refresh bumps `thumbnail_revision` rather than changing the
handle mid-activity.
"""

from __future__ import annotations

import json
import time
from typing import Any

from frigate_sidecar.push.payload import APNS_MAX_PAYLOAD_BYTES, body_text, pretty_label
from frigate_sidecar.push.situations import (
    STAGE_ARRIVING,
    STAGE_ENDING,
    STAGE_ESCALATED,
    Match,
)

#: The app's `ActivityAttributes` conformer. iOS matches the start push to a
#: registered attributes type by this name, so it must equal the Swift type.
ATTRIBUTES_TYPE = "SituationActivityAttributes"

#: How long the activity lingers after resolution before iOS fades it.
DEFAULT_DISMISSAL_TAIL_S = 30.0
#: A shorter tail for an activity that never earned its place -- an early-fire
#: start off a `detection` review that never promoted to `alert`.
UNPROMOTED_DISMISSAL_TAIL_S = 10.0


def content_state(
    match: Match,
    *,
    stage: str,
    thumbnail_revision: int = 1,
) -> dict[str, Any]:
    """The dynamic half, identical in shape on every push that carries state."""
    return {
        "stage": stage,
        "dwell_seconds": int(match.dwell_s),
        "title": match.situation.name or match.situation.id,
        "subtitle": _subtitle(match, stage),
        "thumbnail_revision": thumbnail_revision,
    }


def _subtitle(match: Match, stage: str) -> str:
    """The line under the title.

    Reuses Phase 1's alert-body authoring so an activity that escalates into a
    banner doesn't visibly change its wording halfway through -- the user is
    meant to experience one thing evolving, not two descriptions of it.
    """
    if stage == STAGE_ARRIVING:
        # No dwell worth quoting yet: "Person, 0s" reads as broken.
        label = pretty_label(match.label) or "Motion"
        return f"{label} just arrived" if not match.audio else pretty_label(match.audio)
    if stage == STAGE_ENDING:
        return "Cleared"
    return body_text(match)


def attributes(match: Match, *, handle: str, camera: str) -> dict[str, Any]:
    """The static half -- fixed when the activity is created, never updated."""
    return {
        "situation_id": match.situation.id,
        "situation_name": match.situation.name or match.situation.id,
        "camera_id": camera,
        "handle": handle,
        "track_id": match.track_id,
    }


def build_start(
    match: Match,
    *,
    handle: str,
    camera: str,
    server_id: str,
    thumbnail_revision: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """The push that asks iOS to create the activity."""
    sent_at = time.time() if now is None else now
    state = content_state(match, stage=STAGE_ARRIVING, thumbnail_revision=thumbnail_revision)
    payload = {
        "aps": {
            # Whole seconds: `aps.timestamp` is APNs' own staleness field for
            # live activities, distinct from our `sent_at` telemetry.
            "timestamp": int(sent_at),
            "event": "start",
            "content-state": state,
            "attributes-type": ATTRIBUTES_TYPE,
            "attributes": attributes(match, handle=handle, camera=camera),
            # The one LA push with an alert body. iOS uses it for the
            # lock-screen presentation when the activity first appears; it is
            # not a banner and does not buzz.
            "alert": {"title": state["title"], "body": state["subtitle"]},
        },
        "situation_id": match.situation.id,
        "handle": handle,
        "server_id": server_id,
        "sent_at": round(sent_at, 3),
    }
    return _fit(payload)


def build_update(
    match: Match,
    *,
    stage: str,
    thumbnail_revision: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """A silent state change. No `alert` key -- that is what makes it silent."""
    sent_at = time.time() if now is None else now
    payload = {
        "aps": {
            "timestamp": int(sent_at),
            "event": "update",
            "content-state": content_state(
                match, stage=stage, thumbnail_revision=thumbnail_revision
            ),
        },
        "sent_at": round(sent_at, 3),
    }
    return _fit(payload)


def build_escalation(
    match: Match,
    *,
    sound: str,
    thumbnail_revision: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """The escalation: one push that advances the activity *and* buzzes.

    An `update`-shaped live-activity push carrying an `alert` sub-key at the
    `aps` level. iOS 17.2+ delivers this as a single event -- the ContentState
    moves to `.escalated`, the banner shows, the sound plays -- which is what
    "one thing evolving, not two events" has to mean in practice.

    This replaces the Phase 1-shape alert push the sidecar used to send here.
    An alert push with a matching `apns-collapse-id` collapses in Notification
    Center but *cannot* advance a Live Activity's ContentState, so the two
    surfaces would have drifted apart: a banner saying the situation escalated
    over an activity still rendering `.present`.
    """
    sent_at = time.time() if now is None else now
    state = content_state(match, stage=STAGE_ESCALATED, thumbnail_revision=thumbnail_revision)
    payload = {
        "aps": {
            "timestamp": int(sent_at),
            "event": "update",
            "content-state": state,
            # The two keys that make this one different from a silent update.
            "alert": {"title": state["title"], "body": state["subtitle"]},
            "sound": sound,
            # Not in the amended plan, kept from plan §3: `.timeSensitive` is
            # what lets an escalation break through a Focus mode, and an
            # escalation the user configured is exactly the thing that should.
            "interruption-level": "time-sensitive",
        },
        "sent_at": round(sent_at, 3),
    }
    return _fit(payload)


def build_end(
    match: Match,
    *,
    thumbnail_revision: int = 1,
    tail_s: float = DEFAULT_DISMISSAL_TAIL_S,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolution, with the tail the activity fades over."""
    sent_at = time.time() if now is None else now
    payload = {
        "aps": {
            "timestamp": int(sent_at),
            "event": "end",
            "content-state": content_state(
                match, stage=STAGE_ENDING, thumbnail_revision=thumbnail_revision
            ),
            # Epoch seconds; iOS fades the activity at this moment rather than
            # yanking it off the screen the instant the object left frame.
            "dismissal-date": int(sent_at + tail_s),
        },
        "sent_at": round(sent_at, 3),
    }
    return _fit(payload)


def payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode())


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the two author-controlled strings if a payload nears APNs' cap.

    Live-activity payloads have the same 4KB ceiling as alerts, and the only
    unbounded strings in this shape are the situation name (which the user
    types) and the subtitle derived from it.
    """
    if payload_size(payload) <= APNS_MAX_PAYLOAD_BYTES:
        return payload
    state = payload["aps"].get("content-state")
    if isinstance(state, dict):
        state["title"] = str(state.get("title", ""))[:120]
        state["subtitle"] = str(state.get("subtitle", ""))[:180]
    alert = payload["aps"].get("alert")
    if isinstance(alert, dict):
        alert["title"] = str(alert.get("title", ""))[:120]
        alert["body"] = str(alert.get("body", ""))[:180]
    return payload
