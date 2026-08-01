"""Plain dataclasses shared across the push-notification pipeline.

Kept dependency-free (no FastAPI/pydantic imports) so `decision.py` stays
trivially unit-testable -- it never has to construct an app or a DB
connection to be exercised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("detection", "alert")  # low -> high, matches Frigate's own field


@dataclass(frozen=True)
class ReviewEvent:
    """A `frigate/reviews` MQTT message, reduced to what the decision engine
    and handle minting need. Not the raw Frigate event id -- see `event_id`.
    """

    review_id: str
    camera: str
    severity: str  # "alert" | "detection"
    labels: tuple[str, ...] = field(default_factory=tuple)
    msg_type: str = "new"  # "new" | "update"
    # The Frigate *event* id this review item is about (from
    # `after.data.detections[0]`), distinct from `review_id` (`after.id`).
    # Falls back to `review_id` if Frigate ever sends a review with no
    # detections attached yet.
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(self, "event_id", self.review_id)


@dataclass(frozen=True)
class Device:
    """One registered push-notification device (`push_devices` row)."""

    apns_token: str
    device_id: str
    bundle_id: str
    environment: str  # "sandbox" | "prod"
    app_version: str = ""
    cameras: tuple[str, ...] = field(default_factory=tuple)  # () = all cameras
    labels: tuple[str, ...] = field(default_factory=tuple)  # () = all labels
    min_severity: str = "alert"
