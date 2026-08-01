"""Notification decision engine: parse MQTT review payloads, decide who gets
pushed.

Two pure, dependency-free functions carry the whole contract:

* `parse_review_message` -- turn a `frigate/reviews` MQTT payload into a
  `ReviewEvent`, or `None` if it isn't actionable.
* `devices_for_event` -- which registered devices' subscription filters
  (camera / label / severity) match a given event.

Doing this match at the sidecar, where the MQTT payload already lives, means
a device that wants doorbell-only alerts never causes a send for the garden
camera -- battery, and keeps "the sidecar sees the review id and camera, not
the scene" as tight as possible even internally (spec §1).
"""

from __future__ import annotations

from typing import Any

from frigate_sidecar.push.models import SEVERITIES, Device, ReviewEvent

_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def parse_review_message(payload: dict[str, Any]) -> ReviewEvent | None:
    """Parse one `frigate/reviews` message.

    Fires on `type in ("new", "update")` with `after.severity == "alert"` or
    `"detection"`; `type == "end"` finalizes a review item and is not itself
    pushed (spec's "Architecture at a glance"). Returns `None` for anything
    not actionable rather than raising -- a malformed or unrecognised message
    should be dropped, not crash the subscriber loop.
    """
    msg_type = payload.get("type")
    if msg_type not in ("new", "update"):
        return None

    after = payload.get("after") or {}
    if not isinstance(after, dict):
        return None

    severity = after.get("severity")
    if severity not in SEVERITIES:
        return None

    review_id = after.get("id")
    camera = after.get("camera")
    if not review_id or not camera:
        return None

    data = after.get("data") or {}
    objects = data.get("objects") or []
    labels = tuple(str(o) for o in objects if o)

    detections = data.get("detections") or []
    event_id = str(detections[0]) if detections else str(review_id)

    return ReviewEvent(
        review_id=str(review_id),
        camera=str(camera),
        severity=str(severity),
        labels=labels,
        msg_type=str(msg_type),
        event_id=event_id,
    )


def matches(device: Device, event: ReviewEvent) -> bool:
    """True if `device`'s subscription filters admit `event`.

    Filters are a subscription, not a client-side discard (spec §1): an empty
    `cameras`/`labels` list means "all", matching the registration contract's
    "omit or [] = all cameras/labels".
    """
    if _SEVERITY_RANK[event.severity] < _SEVERITY_RANK[device.min_severity]:
        return False
    if device.cameras and event.camera not in device.cameras:
        return False
    return not (device.labels and not (set(device.labels) & set(event.labels)))


def devices_for_event(devices: list[Device], event: ReviewEvent) -> list[Device]:
    """Registered devices whose filters match `event`, in input order."""
    return [d for d in devices if matches(d, event)]
