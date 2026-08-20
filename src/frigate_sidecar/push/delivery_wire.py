"""Wires the delivery pipeline (`delivery.py`, `cards.py`) to the sidecar's
existing Frigate event flow (Elsinore Phase 2, config-gated, default off).

No new transport, no new MQTT subscription: `PushEngine.handle_event` /
`handle_object_payload` are already the two entry points every
`frigate/reviews` and `frigate/events` message passes through, so this
module is a plain function each of them calls once, guarded by
`settings.push.delivery_enabled`.

**Subject classification here is a deliberate MVP**, not the full-fidelity
mapping the design doc's ladder deserves -- `frigate/reviews` carries labels
but no resolved identity (Phase 5's territory), so `classify_subject` is a
heuristic, documented as such, safe to ship because the whole pipeline is
off by default. **Place classification** (`classify_place`) is Phase 4's:
it reads the user's own `settings.zone_classes`
(`push/policy_settings.py`), falling back to the same name-guessing
heuristic the settings API exposes for a zone nobody has classified yet.
Tightening `classify_subject` is a data/code change here, not a change to
`ladder.py` or `delivery.py`, exactly like `ladder_policy.py`'s own
separation of policy from evaluation order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from frigate_sidecar.push import (
    card_store,
    decision_trace,
    ground,
    live_activities,
    policy_settings,
    store,
)
from frigate_sidecar.push.cards import CREATE, DEESCALATE, ENRICH, ESCALATE, RESOLVE, Card
from frigate_sidecar.push.cards import SUPPRESSED as SUPPRESSED_MUTATION
from frigate_sidecar.push.delivery import (
    _device_eligible,
    _is_snoozed,
    build_card_key,
    build_card_payload,
    send_card_mutation,
    should_push,
    sound_name_for_card,
)
from frigate_sidecar.push.delivery import advance_card as _advance_card
from frigate_sidecar.push.ladder import SUPPRESSED, Snapshot, evaluate_ladder
from frigate_sidecar.push.models import ReviewEvent
from frigate_sidecar.push.payload import pretty_label

if TYPE_CHECKING:
    import sqlite3

    from frigate_sidecar.config import PushSection
    from frigate_sidecar.push.engine import PushEngine
    from frigate_sidecar.push.models import Device
    from frigate_sidecar.push.transport import PushTransport

#: Mutations that get a snapshot -- the card is still active and its
#: bounding box/identity may still be refining. Never `escalate`/
#: `deescalate` (unrequested scope this round) and never `resolve`: a
#: resolved card is about to leave Notification Center, so spending the
#: NSE's ~15s fetch budget on an image nobody will see is pure waste.
_MEDIA_MUTATIONS = frozenset({CREATE, ENRICH})

#: Cross-camera dedup window (docs/push-notifications.md "Cross-camera
#: deduplication"). Real-world data showed 3-4s gaps between overlapping
#: cameras picking up the same walk-through, but detection latency varies by
#: camera and lighting; 15s catches slow detections without false-merging
#: genuinely separate events arriving well apart. No config knob for v1 --
#: a constant like every other MVP threshold in this module.
_DEDUP_WINDOW_S = 15.0

#: §8 LA cadence: minimum seconds between content-state pushes per activity.
_LA_UPDATE_MIN_INTERVAL_S = 3.0
#: §8 path growth threshold: push only when this many new points have arrived.
_LA_PATH_GROWTH_THRESHOLD = 3

#: Place-class ordering outermost→innermost, for the zones.ladder.
_PLACE_ORDER = ("street", "yard", "doors", "private", "off_limits")

logger = logging.getLogger(__name__)

#: Per-(card_key, apns_token) snapshot of the last LA push, for delta
#: detection. In-memory only — a sidecar restart flushes it, which just
#: means the first post-restart push always goes out (safe).
_la_prev_state: dict[tuple[str, str], dict[str, Any]] = {}

#: Frigate labels this MVP treats as an animal subject, beyond the
#: dangerous-animal labels `ladder.py` already reclassifies via `label`
#: regardless of what subject is passed in.
_ANIMAL_LABELS = frozenset({"dog", "cat", "bird", "deer", "squirrel", "raccoon", "bear", "skunk"})
_VEHICLE_LABELS = frozenset({"car", "motorcycle", "bicycle"})

_SUBJECT_GLYPH = {
    "stranger": "person.stranger",
    "known": "person.identified",
    "person": "person.detected",
    "vehicle": "vehicle.detected",
    "animal": "animal.seen",
    "thing": "thing.detected",
}

_SUBJECT_COPY = {
    "stranger": "Person",
    "known": "Person",
    "person": "Person",
    "vehicle": "Vehicle",
    "animal": "Animal",
}


def classify_subject(event: ReviewEvent) -> str:
    """Observable-subject classification (routing v2): label, camera, zone
    only. Identity (sub_label/plate) is never consulted at create time —
    it arrives later via recognition and only relaxes a running story."""
    labels = set(event.labels)
    if "person" in labels:
        return "person"
    if labels & _VEHICLE_LABELS:
        return "vehicle"
    if labels & _ANIMAL_LABELS:
        return "animal"
    return "thing"


def classify_place(event: ReviewEvent, zone_classes: dict[str, str]) -> str:
    """`zone_classes` (Elsinore Phase 4: `push/policy_settings.py`,
    `settings.zone_classes`) is the user's explicit zone -> place-class
    assignment; a zone not in it falls back to the same name-guessing
    heuristic the settings API's `available_zones` uses
    (`policy_settings.guess_zone_class`), rather than the flat "yard" this
    used to default to -- new zones (a camera the user just added) get a
    reasonable class immediately instead of going silent until explicitly
    configured."""
    for zone in event.zones:
        if zone in zone_classes:
            return zone_classes[zone]
    if event.zones:
        return policy_settings.guess_zone_class(event.zones[0])
    return "street"


def snapshot_from_review(
    event: ReviewEvent,
    *,
    zone_classes: dict[str, str],
    nobody_home: bool = False,
    night: bool = False,
    dwell_exceeded: bool = False,
    muted: bool = False,
) -> tuple[Snapshot, str, str]:
    """Build the ladder `Snapshot`, returning `(snapshot, subject_kind,
    place_class)` since the payload contract needs the classification
    alongside the level the snapshot evaluates to."""
    subject_kind = classify_subject(event)
    place_class = classify_place(event, zone_classes)
    zone = event.zones[0] if event.zones else ""
    label = event.labels[0] if event.labels else ""
    snapshot = Snapshot(
        subject=subject_kind, place=place_class, zone=zone, label=label,
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded, muted=muted,
    )
    return snapshot, subject_kind, place_class


def _fmt_elapsed(elapsed_s: float) -> str:
    """Human elapsed for notification copy: "just now" under a minute, then
    minutes, then h/m — raw "137s" read like debug output."""
    s = int(elapsed_s)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} hr {(s % 3600) // 60} min"


def _copy(
    subject_kind: str, label: str, camera: str, zone_name: str, elapsed_s: float,
    identity: str = "",
    story: str = "",
) -> tuple[str, str]:
    """Title + body (content design 2026-08-15).

    Title: "{Who} in {zone display name}" when the event has a zone —
    the sidecar-edited `zone_names` map (or Frigate `friendly_name`) supplies
    a real place phrase; a bare rule key is humanized as a last resort. With
    no zone the camera never masquerades as a place: "{Who} · {Camera} camera".

    Body ("notable verbs only"): when `story` is set (approaching / running /
    still there / left after …) it leads; otherwise camera (when the title
    used a zone) + friendly elapsed. A line with nothing new stays empty.
    """
    subject_text = _SUBJECT_COPY.get(subject_kind) or pretty_label(label) or "Motion"
    who = identity or subject_text
    camera_pretty = camera.replace("_", " ").title()
    if zone_name:
        friendly = policy_settings.zone_display_name(zone_name)
        place = friendly or zone_name.replace("_", " ").title()
        primary = f"{who} in {place}"
        detail = f"{camera_pretty} camera" if camera else ""
    else:
        primary = f"{who} · {camera_pretty} camera" if camera else who
        detail = ""

    parts = [p for p in (story, detail, _fmt_elapsed(elapsed_s) if elapsed_s > 0 else "") if p]
    return primary, " · ".join(parts)


def _glyph_for(subject_kind: str, label: str) -> str:
    if label:
        return f"{subject_kind}.{label}"
    return _SUBJECT_GLYPH.get(subject_kind, "motion.detected")


#: Normalized-image-space displacement below which movement is jitter, not
#: travel. q10 of real person path steps measured at 0.008, median 0.066
#: (tools/verify_heading.py — since removed, git history — over config captures, 2026-08-15).
_HEADING_MIN_DISPLACEMENT = 0.02


def _movement_vector(
    path_data: Sequence[tuple[float, ...]] | None,
) -> tuple[float, float] | None:
    """Recent direction of travel as a unit vector in normalized image
    space (y down), from the track's path trail: walk back from the newest
    point until the displacement clears the jitter floor. None while the
    trail is too short or the subject hasn't really moved."""
    if not path_data or len(path_data) < 2:
        return None
    x1, y1 = path_data[-1][0], path_data[-1][1]
    for point in reversed(path_data[:-1]):
        dx, dy = x1 - point[0], y1 - point[1]
        dist = math.hypot(dx, dy)
        if dist >= _HEADING_MIN_DISPLACEMENT:
            return (dx / dist, dy / dist)
    return None


def _heading_label(
    path_data: Sequence[tuple[float, ...]] | None, stationary: bool, camera: str,
) -> str | None:
    """One of the §8 heading words, or None when unknown.

    Measured 2026-08-15 (tools/verify_heading.py, git history; 24,572 captured events):
    this install's Frigate reports velocity_angle=0 / speed=0 on every
    message (no zone distance calibration), so the old angle thresholds
    were reading a constant — every moving subject showed "leaving".
    Heading now comes from the path trail dotted against the per-camera
    vector the user draws on /cameras ("toward home"); an uncalibrated
    camera honestly shows no chip rather than a guess."""
    if stationary:
        return "stationary"
    movement = _movement_vector(path_data)
    if movement is None:
        return None
    calib = policy_settings.get_active().get("camera_headings", {}).get(camera)
    if not isinstance(calib, dict):
        # No hand-drawn vector: fall back to the one derived from world
        # geometry (pie azimuth + secure area on the /cameras map).
        calib = policy_settings.derived_camera_heading(camera)
    if not isinstance(calib, dict):
        return None
    dot = movement[0] * calib.get("dx", 0.0) + movement[1] * calib.get("dy", 0.0)
    # cos 60° = 0.5: within 60° of the drawn vector = approaching; within
    # 60° of its opposite = leaving; the perpendicular band = passing.
    if dot >= 0.5:
        return "approaching"
    if dot <= -0.5:
        return "leaving"
    return "passing"


def _build_motion(
    path_data: Sequence[tuple[float, ...]] | None, stationary: bool, camera: str,
    speed_label: str | None = None,
) -> dict[str, str] | None:
    heading = _heading_label(path_data, stationary, camera)
    if heading is None:
        return None
    motion: dict[str, str] = {"heading": heading}
    if speed_label and heading not in ("stationary",):
        motion["speed_label"] = speed_label
    return motion


#: Per-(camera, track) consecutive-heading counter for sustained-direction
#: routing. In-memory like `_la_prev_state`; a restart just resets streaks.
_heading_streaks: dict[tuple[str, str], tuple[str | None, int]] = {}


def _update_heading_streak(camera: str, track_id: str, heading: str | None) -> int:
    prev, count = _heading_streaks.get((camera, track_id), (None, 0))
    count = count + 1 if (heading is not None and heading == prev) else (1 if heading else 0)
    _heading_streaks[(camera, track_id)] = (heading, count)
    return count


def last_heading(camera: str, track_id: str) -> str | None:
    """The most recent heading observed for this track (engine reads this
    to shorten the LA dismissal tail on 'leaving')."""
    return _heading_streaks.get((camera, track_id), (None, 0))[0]


#: Per-(camera, track) last ANNOUNCED distance to the secure area, in feet.
#: In-memory like the streaks; a restart just re-announces.
_announced_distance: dict[tuple[str, str], int] = {}


def _round_distance_ft(ft: float) -> int:
    """Copy-grade rounding: nearest 5 ft, floored at 5 (the exact number is
    projection-model precision theater below that)."""
    return max(5, int(round(ft / 5.0)) * 5)


def _approach_story(nearest_ft: int | None, speed_words: set[str]) -> str:
    """Copy for a confirmed approach. With a calibrated world model this
    says how far out ("approaching — 30 ft out, walking"); beyond 100 ft
    the number is projection-error noise and the classic phrase stands."""
    if nearest_ft is not None and nearest_ft == 0:
        return "at the house"
    if nearest_ft is not None and nearest_ft <= 100:
        pace = (
            ", running" if "running" in speed_words
            else ", walking" if "walking" in speed_words else ""
        )
        return f"approaching — {nearest_ft} ft out{pace}"
    return "approaching the house"


def _stabilize_distance(camera: str, track_id: str, raw_ft: float) -> int:
    """Rounded distance with hysteresis: keep announcing the previous value
    until the raw distance moves ≥10 ft from it, so copy never flaps
    30 → 25 → 30 across consecutive card mutations. 0 (inside the secure
    area) always announces immediately."""
    key = (camera, track_id)
    prev = _announced_distance.get(key)
    if raw_ft <= 2.5:
        _announced_distance[key] = 0
        return 0
    if prev is not None and prev > 0 and abs(raw_ft - prev) < 10.0:
        return prev
    out = _round_distance_ft(raw_ft)
    _announced_distance[key] = out
    return out


def _build_zones_ladder(
    event_zones: tuple[str, ...],
    current_zones: tuple[str, ...],
    zone_classes: dict[str, str],
) -> dict[str, Any] | None:
    """Build zones.ladder (display names outermost→innermost) and current_index."""
    if not event_zones:
        return None
    ordered: list[tuple[int, str]] = []
    for z in event_zones:
        pc = zone_classes.get(z) or policy_settings.guess_zone_class(z)
        idx = _PLACE_ORDER.index(pc) if pc in _PLACE_ORDER else 1
        ordered.append((idx, z))
    ordered.sort(key=lambda t: t[0])
    ladder = [z.replace("_", " ").title() for _, z in ordered[:5]]
    zone_names = [z for _, z in ordered[:5]]
    current_index = -1
    for i, name in enumerate(zone_names):
        if name in current_zones:
            current_index = i
    return {"ladder": ladder, "current_index": current_index}


def _build_la_path(
    path_data: list[tuple[float, float, float]],
) -> dict[str, Any] | None:
    if not path_data:
        return None
    # Wire contract stays [x, y] pairs (4KB ContentState budget) — the
    # per-point timestamp is server-side fuel (speed), never shipped.
    points = live_activities.downsample_path(
        [[pt[0], pt[1]] for pt in path_data],
    )
    if not points:
        return None
    return {"points": points}


def _la_has_visible_delta(
    *,
    mutation: str,
    prev_mutation: str | None,
    prev_level: str | None,
    level: str,
    primary: str,
    prev_primary: str | None,
    glyph: str,
    prev_glyph: str | None,
    current_zones: tuple[str, ...],
    prev_zones: tuple[str, ...] | None,
    path_len: int,
    prev_path_len: int,
    heading: str | None,
    prev_heading: str | None,
) -> str | None:
    """Return the delta reason if there's a visible change worth pushing,
    or None to suppress the push."""
    if mutation in (CREATE, ESCALATE, DEESCALATE, RESOLVE):
        return mutation
    if level != prev_level:
        return "level_change"
    if primary != prev_primary:
        return "text_change"
    if glyph != prev_glyph:
        return "glyph_change"
    if current_zones != prev_zones:
        return "zone_transition"
    if heading is not None and heading != prev_heading:
        return "heading_change"
    if path_len - prev_path_len >= _LA_PATH_GROWTH_THRESHOLD:
        return "path_growth"
    return None


def _media_for(
    mutation: str,
    event: ReviewEvent,
    *,
    conn: sqlite3.Connection,
    engine: PushEngine | None,
    config: PushSection,
) -> tuple[str | None, str | None, asyncio.Task[bool] | None]:
    """Mint a handle and kick off the snapshot pre-warm for a mutation that
    gets media (`_MEDIA_MUTATIONS`), returning `(handle, media_url,
    warm_task)`.

    All three are `None` when there's nothing to show: `escalate`/
    `deescalate`/`resolve` never get one (a resolved card is about to leave
    Notification Center -- spending the NSE's ~15s fetch budget on an image
    nobody will see is pure waste), and neither does a mutation that would,
    absent `config.external_base_url` or an `engine` to pre-warm through
    (e.g. a caller only exercising the pure classifier). The bare `handle`
    is returned alongside the full URL because the Live Activity content-
    state (Phase 3) wants just the handle -- the widget already knows its
    own base URL, unlike the card push's self-contained `media` field.

    Same mechanism as the situations path (`PushEngine._fire_group`): mint
    the handle synchronously, fire the Frigate fetch as a background task
    the caller runs concurrently with the send, never in series -- the push
    already carries the URL optimistically, so a slow/failed fetch costs the
    notification its image, not its existence.
    """
    if mutation not in _MEDIA_MUTATIONS or not config.external_base_url or engine is None:
        return None, None, None
    handle = store.mint_handle(
        conn, camera=event.camera, event_id=event.event_id, review_id=event.review_id,
        ttl_s=config.situation_handle_ttl_s,
    )
    conn.commit()
    media = f"{config.external_base_url.rstrip('/')}/v1/push/thumbnail/{handle}"
    warm_task = asyncio.create_task(
        engine.prewarm_thumbnail(handle, camera=event.camera, event_id=event.event_id)
    )
    return handle, media, warm_task


def _resolve_card_for_track(
    conn: sqlite3.Connection,
    *,
    camera: str,
    track_id: str,
    subject_kind: str,
    zone_name: str,
    zones: tuple[str, ...] = (),
    now: float,
    geo_mates: list[tuple[str, str]] | None = None,
    geo_enabled: bool = False,
) -> tuple[str, Card | None, str, bool]:
    """Which card this (camera, track_id) evaluation belongs to, applying
    cross-camera dedup (docs/push-notifications.md "Cross-camera
    deduplication") before a fresh card key would otherwise be minted.

    Returns `(card_key, existing_card_or_None, owning_camera, via_geo)`.
    `owning_camera` is this track's own camera unless the track has been
    merged onto a card another camera created first, in which case it's
    that card's original camera -- callers must persist *that*, not
    `camera`, so a merged card's identity/timeline routing never flips to
    whichever camera happened to enrich it most recently.

    Three paths, checked in order:

    1. This track already has an alias (a prior evaluation merged it onto
       another camera's card) whose target is still open -- keep using it.
       A stale alias (target since closed/resolved) is dropped so this
       track falls through to its own natural key, per the "a fresh card if
       still detected" rule (design doc): once the merged card is gone,
       there's nothing left to enrich.
    2. No alias, and this is a genuinely new track (no row yet under its own
       natural key) with a zone: look for an open card with the same
       `subject_kind`/`zone_name` created within the dedup window. If one
       exists, alias this track onto it instead of creating a sibling.
    3. No zone match, but geometry clusters this track with another
       camera's track that owns an open same-label card (`geo_mates`, from
       fusion.cluster) -- adopt that card WHEN the `geometric_dedup` policy
       flag is on. Flag off: log what would have been adopted
       ("geometric_dedup: would_suppress ...") so a week of logs can be
       grepped against actual duplicate cards before enabling.
    4. Otherwise (existing card under its own key, or no zone to dedup on)
       -- this track's own natural key, unchanged from before this feature.
    """
    alias_key = card_store.get_track_alias(conn, camera, track_id)
    if alias_key is not None:
        aliased = card_store.get_card(conn, alias_key)
        if aliased is not None and not aliased.closed:
            ctx = card_store.get_card_context(conn, alias_key)
            return alias_key, aliased, (ctx or {}).get("camera") or camera, False
        card_store.delete_track_alias(conn, camera, track_id)

    natural_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    existing = card_store.get_card(conn, natural_key)
    if existing is None:
        # Label flip (animal -> person on the same track): keep the story on
        # its original card instead of minting a sibling. The card keeps its
        # birth key (collapse-id stability); subject_kind context updates on
        # the next upsert, so copy/routing follow the new label.
        flipped_key = card_store.find_open_card_for_track(
            conn, camera=camera, track_id=track_id, exclude_key=natural_key,
        )
        if flipped_key is not None:
            flipped = card_store.get_card(conn, flipped_key)
            if flipped is not None and not flipped.closed:
                logger.info(
                    "push: label flip keeps card=%s (was routing as %s)",
                    flipped_key, natural_key,
                )
                ctx = card_store.get_card_context(conn, flipped_key)
                return flipped_key, flipped, (ctx or {}).get("camera") or camera, False
    neighbor_cameras = policy_settings.camera_neighbor_set(camera)
    if existing is None and (zone_name or neighbor_cameras):
        candidate_key = card_store.find_dedup_candidate(
            conn, subject_kind=subject_kind, zone_name=zone_name,
            exclude_key=natural_key, now=now, window_s=_DEDUP_WINDOW_S,
            zones=zones, neighbor_cameras=neighbor_cameras,
        )
        if candidate_key is not None:
            candidate = card_store.get_card(conn, candidate_key)
            if candidate is not None and not candidate.closed:
                card_store.set_track_alias(conn, camera, track_id, candidate_key, now)
                ctx = card_store.get_card_context(conn, candidate_key)
                return candidate_key, candidate, (ctx or {}).get("camera") or camera, False

    # Geometric adoption: zone dedup found nothing, but fusion clustered
    # this track with another camera's track. Adopt that mate's open card
    # (its alias target, else its own natural card) — the mate saw the same
    # physical object within the distance-scaled merge threshold.
    if existing is None and geo_mates:
        for mate_cam, mate_tid in geo_mates:
            mate_key = card_store.get_track_alias(conn, mate_cam, mate_tid)
            if mate_key is None:
                mate_key = card_store.find_open_card_for_track(
                    conn, camera=mate_cam, track_id=mate_tid, exclude_key="",
                )
            if mate_key is None:
                continue
            mate_card = card_store.get_card(conn, mate_key)
            if mate_card is None or mate_card.closed:
                continue
            if not geo_enabled:
                # Validation breadcrumb — fires with the flag OFF so the
                # operator can count would-suppress vs. real duplicates.
                logger.info(
                    "geometric_dedup: would_suppress card=%s adopting=%s/%s",
                    natural_key, mate_cam, mate_tid,
                )
                break
            card_store.set_track_alias(conn, camera, track_id, mate_key, now)
            ctx = card_store.get_card_context(conn, mate_key)
            logger.info(
                "geometric_dedup: adopted card=%s for %s/%s", mate_key, camera, track_id,
            )
            return mate_key, mate_card, (ctx or {}).get("camera") or camera, True

    return natural_key, existing, camera, False


async def _deliver_live_activities(
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    *,
    config: PushSection,
    card: Card,
    mutation: str,
    family: str | None,
    camera: str,
    subject_kind: str,
    label: str,
    primary: str,
    secondary: str,
    elapsed_seconds: int,
    media_handle: str | None,
    now: float,
    sound_allowed: bool = True,
    state_since_ts: float | None = None,
    motion: dict[str, Any] | None = None,
    zones: dict[str, Any] | None = None,
    path: dict[str, Any] | None = None,
) -> set[str]:
    """The Live Activity side of one card mutation, one iteration per
    registered device (Elsinore Phase 3, `docs/push-notifications.md` "Live
    Activity lifecycle"). Independent of the ordinary card push above: a
    device with no push-to-start token, or a family the config has turned
    off, simply never gets an activity while its card push is unaffected.

    Reuses the same `push_activities` table and `store.py` helpers the
    (unrelated) situations Live Activity already uses -- `situation_id` is
    just a text column and a card's `card_key` fits it exactly as well as a
    situation id does. This is *why* the row lives there and not as a new
    column on `push_cards`: a Live Activity is one per *(device, card)*, not
    one per card, so a single nullable column on the card row can't
    represent a card with two push-to-start-registered devices running two
    independent activities.

    `family` is `None` for a card that never qualified (in which case this
    is a no-op for every device -- `store.find_activity` never finds a row
    either) or on `resolve` (ending an already-running activity needs no
    re-qualification).

    Returns the apns tokens of devices whose Live Activity *demonstrably*
    covers this mutation: a start APNs accepted, or an update/end sent to a
    confirmed per-activity token. The caller demotes the ordinary card push
    to silent for exactly those devices -- per-device, so one phone's
    working LA never silences another's banner, and gated on confirmation
    (not intent) so a silently-failed LA never eats a notification.
    """
    covered: set[str] = set()
    if not config.delivery_la_enabled:
        logger.info("push: LA skipped — delivery_la_enabled=False")
        return covered
    logger.info(
        "push: LA enter mutation=%s family=%s card_key=%s devices=%d",
        mutation, family, card.card_key, len(devices),
    )
    card_key = card.card_key
    # The primary/original subject id, stable across cross-camera dedup --
    # `card_key` is always `{camera}:{subject_kind}:{subject_id}`, so this is
    # the same value regardless of which camera's event triggered this call.
    la_track_id = card_key.split(":", 2)[-1]

    policy = policy_settings.get_active()
    la_settings = policy.get("live_activities", {})
    alert_all = la_settings.get("alert_all_changes", False)
    la_only = la_settings.get("la_only", False)
    escalation_sound = policy.get("escalation_sound", "urgent")
    stale_s = config.delivery_la_stale_s

    for device in devices:
        row = store.find_activity(
            conn, apns_token=device.apns_token, situation_id=card_key, track_id=la_track_id,
        )

        if mutation == RESOLVE:
            if row is None:
                continue
            content_state = live_activities.build_content_state(
                level=card.level, mutation=mutation,
                glyph=live_activities.glyph_for(
                    family or "", subject_kind=subject_kind, label=label, mutation=mutation,
                ),
                primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
                card_key=card_key, thumbnail_handle=None,
                thumbnail_revision=int(row["thumbnail_revision"] or 1),
                state_since_ts=state_since_ts,
                motion=motion, zones=zones, path=path,
            )
            payload = live_activities.build_la_end_payload(
                content_state=content_state, now=now, dismissal_offset=30.0,
            )
            token = row["token"]
            if token:
                result = await transport.send_live_activity(
                    device, token=token, payload=payload, collapse_id=card_key, event="end",
                    apns_priority=5,
                )
                if result.ok:
                    covered.add(device.apns_token)
                store.close_activity(conn, row["activity_id"], now=now)
                _la_prev_state.pop((card_key, device.apns_token), None)
            else:
                # Fast create→resolve: the app hasn't uploaded the
                # per-activity token yet, and iOS only accepts `start` on
                # the p2s token (commit 0916731) — an end sent there is
                # rejected. Leave the row open, flagged pending-end; the
                # token-upload route sends the deferred end the moment the
                # token lands, instead of stranding the LA on the lock
                # screen until its stale-date.
                store.touch_activity(conn, row["activity_id"], stage="pending_end", now=now)
            continue

        if row is None:
            # CREATE is the normal birth; ESCALATE is the late start — a
            # story that routed quiet at create didn't qualify for a person
            # LA (routing-gated families), but the moment it escalates into
            # notify/urgent it deserves its instrument. The start push's
            # mandatory alert (with sound, budget permitting) doubles as
            # the escalation alert itself.
            if mutation not in (CREATE, ESCALATE) or family is None or not device.can_live_activity:
                logger.info(
                    "push: LA skip device=%s reason=no_row mutation=%s family=%s"
                    " la_capable=%s pts=%s",
                    device.device_id, mutation, family, device.la_capable,
                    bool(device.push_to_start_token),
                )
                continue
            if not _device_eligible(device, camera=camera, labels=(label,), card_level=card.level):
                logger.info("push: LA skip device=%s reason=not_eligible", device.device_id)
                continue
            if _is_snoozed(conn, device, camera, now=now):
                logger.info("push: LA skip device=%s reason=snoozed", device.device_id)
                continue
            content_state = live_activities.build_content_state(
                level=card.level, mutation=mutation,
                glyph=live_activities.glyph_for(
                    family, subject_kind=subject_kind, label=label, mutation=mutation,
                ),
                primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
                card_key=card_key, thumbnail_handle=media_handle, thumbnail_revision=1,
                state_since_ts=state_since_ts,
                motion=motion, zones=zones, path=path,
            )
            # Honor the card path's sound accounting (mute, quiet hours,
            # per-card budget): the start's required `alert` dict stays,
            # but sound is only attached when a card push would have
            # sounded too.
            la_start_sound = (
                sound_name_for_card(card.level, subject_kind, label,
                                    escalation_sound=escalation_sound)
                if sound_allowed and not la_only else None
            )
            payload = live_activities.build_la_start_payload(
                content_state=content_state, family=family, camera=camera,
                track_id=la_track_id, card_key=card_key, now=now, stale_s=stale_s,
                sound=la_start_sound,
            )
            logger.info(
                "push: LA start device=%s family=%s pts_token=%s...",
                device.device_id, family, (device.push_to_start_token or "")[:16],
            )
            result = await transport.send_live_activity(
                device, token=device.push_to_start_token, payload=payload,
                collapse_id=card_key, event="start",
                apns_priority=10, apns_expiration=int(now + 900),
            )
            logger.info(
                "push: LA start result ok=%s error=%s", result.ok, result.error,
            )
            if not result.ok:
                continue
            covered.add(device.apns_token)
            activity_id = f"a_{secrets.token_urlsafe(8)}"
            store.open_activity(
                conn, activity_id=activity_id, apns_token=device.apns_token,
                situation_id=card_key, track_id=la_track_id, camera=camera,
                collapse_id=card_key, handle=media_handle or "", now=now,
            )
            store.record_activity_send(conn, activity_id=activity_id, now=now)
            continue

        if not row["token"]:
            # No per-activity token yet (app hasn't confirmed the start) --
            # keep the row alive so the sweep doesn't reap a young LA.
            store.touch_activity(conn, row["activity_id"], now=now)
            continue
        revision = int(row["thumbnail_revision"] or 1)
        if media_handle:
            revision += 1
        # §8 delta detection: suppress LA pushes that carry no visible change.
        glyph_val = live_activities.glyph_for(
            family or "", subject_kind=subject_kind, label=label, mutation=mutation,
        )
        heading_val = motion.get("heading") if motion else None
        current_zones_tuple = tuple(zones["ladder"]) if zones else ()
        path_len = len(path["points"]) if path else 0
        prev_key = (card_key, device.apns_token)
        prev = _la_prev_state.get(prev_key, {})
        delta_reason = _la_has_visible_delta(
            mutation=mutation,
            prev_mutation=prev.get("mutation"),
            prev_level=prev.get("level"),
            level=card.level,
            primary=primary,
            prev_primary=prev.get("primary"),
            glyph=glyph_val,
            prev_glyph=prev.get("glyph"),
            current_zones=current_zones_tuple,
            prev_zones=prev.get("zones"),
            path_len=path_len,
            prev_path_len=prev.get("path_len", 0),
            heading=heading_val,
            prev_heading=prev.get("heading"),
        )
        last_la_push = float(row["last_push_at"] or 0)
        if delta_reason is None:
            continue
        if now - last_la_push < _LA_UPDATE_MIN_INTERVAL_S:
            continue
        logger.info("la-push reason=%s card_key=%s", delta_reason, card_key)

        content_state = live_activities.build_content_state(
            level=card.level, mutation=mutation, glyph=glyph_val,
            primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
            card_key=card_key, thumbnail_handle=media_handle, thumbnail_revision=revision,
            state_since_ts=state_since_ts,
            motion=motion, zones=zones, path=path,
        )

        # Escalation alert: when level rises to notify/urgent, the LA update
        # carries an alert dict + sound so iOS surfaces a banner.
        wants_alert = False
        la_sound = None
        la_interruption = None
        if la_only:
            # No banner ever: the LA's own state change (level color, copy)
            # is the whole signal; alert dicts on updates are what banner.
            pass
        elif mutation == ESCALATE and card.level in ("notify", "urgent"):
            wants_alert = True
            if sound_allowed and card.level == "urgent":
                la_sound = sound_name_for_card(card.level, subject_kind, label,
                                              escalation_sound=escalation_sound)
            la_interruption = "time-sensitive" if card.level == "urgent" else "active"
        elif alert_all and mutation == ESCALATE:
            wants_alert = True
        elif family == live_activities.PERSON_RESTRICTED and mutation != RESOLVE:
            wants_alert = True
            la_interruption = "time-sensitive"

        priority = 10 if wants_alert else 5
        payload = live_activities.build_la_update_payload(
            content_state=content_state, now=now, stale_s=stale_s,
            alert=wants_alert, alert_title=primary, alert_body=secondary,
            sound=la_sound, interruption_level=la_interruption,
        )
        result = await transport.send_live_activity(
            device, token=row["token"], payload=payload, collapse_id=card_key, event="update",
            apns_priority=priority, apns_expiration=int(now + 900),
        )
        if result.ok:
            covered.add(device.apns_token)
        store.touch_activity(
            conn, row["activity_id"], thumbnail_revision=revision, pushed=True, now=now,
        )
        store.record_activity_send(conn, activity_id=row["activity_id"], now=now)
        _la_prev_state[(card_key, device.apns_token)] = {
            "mutation": mutation, "level": card.level, "primary": primary,
            "glyph": glyph_val, "zones": current_zones_tuple,
            "path_len": path_len, "heading": heading_val,
        }

    return covered


async def end_activity_if_card_closed(
    conn: sqlite3.Connection,
    device: Device,
    transport: PushTransport,
    *,
    card_key: str,
    track_id: str,
    token: str,
    now: float | None = None,
) -> bool:
    """Deferred end for the fast create→resolve race.

    When a card resolves before the app has uploaded the per-activity token,
    `_deliver_live_activities` can't send the end (iOS rejects end on the
    p2s token) and leaves the row open. The token-upload route calls this
    the moment the token lands: if the card is already closed, end the
    activity now instead of stranding it until its stale-date.
    """
    now = time.time() if now is None else now
    card = card_store.get_card(conn, card_key)
    if card is None or not card.closed:
        return False
    ctx = card_store.get_card_context(conn, card_key) or {}
    kind = ctx.get("subject_kind", "")
    # Resolve copy carries the STORY duration (first sighting -> end), the
    # same clock the LA's frozen timer shows -- not time-in-latest-state,
    # which read "3s" next to an LA frozen at "11s" (observed 2026-08-14).
    # This end is deferred (sent when the token lands, possibly seconds
    # after the story closed), so the clock stops at the card's resolve
    # write (`updated_at`), never at push time.
    elapsed = max(0.0, card.updated_at - card.created_at)
    primary, secondary = _copy(
        kind, "", ctx.get("camera", ""), ctx.get("zone_name", ""), 0.0,
        story=f"left after {_fmt_elapsed(elapsed)}",
    )
    content_state = live_activities.build_content_state(
        level=card.level, mutation=RESOLVE,
        glyph=live_activities.glyph_for("", subject_kind=kind, label="", mutation=RESOLVE),
        primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
        card_key=card_key, thumbnail_handle=None, thumbnail_revision=1,
        state_since_ts=round(card.state_since_at, 1) if card.state_since_at else None,
    )
    payload = live_activities.build_la_end_payload(
        content_state=content_state, now=now, dismissal_offset=30.0,
    )
    result = await transport.send_live_activity(
        device, token=token, payload=payload, collapse_id=card_key, event="end",
        apns_priority=5,
    )
    while True:
        row = store.find_activity(
            conn, apns_token=device.apns_token, situation_id=card_key, track_id=track_id,
        )
        if row is None:
            break
        store.close_activity(conn, row["activity_id"], now=now)
    logger.info(
        "push: LA deferred end card_key=%s ok=%s error=%s", card_key, result.ok, result.error,
    )
    return result.ok


async def handle_delivery_event(
    event: ReviewEvent,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    engine: PushEngine | None = None,
    now: float | None = None,
    nobody_home: bool = False,
    night: bool = False,
    dwell_exceeded: bool = False,
) -> int:
    """One `frigate/reviews` message through the delivery pipeline. Returns
    the number of cards mutated (0 if `delivery_enabled` is off).

    `engine` is only needed to pre-warm a snapshot thumbnail
    (`PushEngine.prewarm_thumbnail`, same as the situations path) -- optional
    so pure-logic-focused callers/tests that don't care about `media` don't
    have to construct one. Without it (or without `config.external_base_url`
    set), `media` is simply omitted, same as "nothing to show".
    """
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now
    policy = policy_settings.get_active()
    escalation_sound = policy.get("escalation_sound", "urgent")

    # Quiet hours (§4): check before ladder evaluation.
    import datetime
    local_now = datetime.datetime.now()
    now_minutes = local_now.hour * 60 + local_now.minute
    qh_active, qh_mode = policy_settings.is_quiet_hours(policy, now_minutes)

    muted = bool(policy.get("mute_sounds"))

    # Motion BEFORE routing (2026-08-15): heading streaks and ground speed
    # are ladder modifiers now, not just LA decoration. Computed once per
    # track here, reused for the LA content build below.
    approaching_secure = False
    leaving_scene = False
    moving_fast = False
    track_motion: dict[str, dict[str, str] | None] = {}
    # Nearest distance (ft) from any of this event's tracks to the secure
    # area, rounded/hysteresis-stabilized for copy; None when the world
    # model can't say. geo_members maps each of this event's tracks to its
    # cross-camera cluster mates (other cameras only), for dedup adoption.
    nearest_ft: int | None = None
    speed_words: set[str] = set()
    geo_members: dict[str, list[tuple[str, str]]] = {}
    if engine is not None:
        # World projection first (was after the motion loop): the motion
        # loop needs per-track map positions for distance-to-secure.
        _scale = policy.get("map_scale_ft")
        _aspect = ground.map_aspect(policy)
        _positions: list = []
        _clusters: list = []
        if _scale and _scale > 0:
            from frigate_sidecar.push import fusion
            _positions = fusion.track_world_positions(
                engine.tracks, policy, now=time.time(),
            )
            _clusters = fusion.cluster(
                _positions, scale_ft=_scale, aspect_h_over_w=_aspect,
            )
        _pos_by_track = {(p.camera, p.track_id): p for p in _positions}

        _raw_nearest: float | None = None
        for _tid in (event.track_ids or (event.event_id,)):
            _ts = engine.tracks.get(event.camera, _tid)
            if _ts is None:
                continue
            _heading = _heading_label(_ts.path_data, _ts.stationary, event.camera)
            _speed = ground.speed_label(ground.speed_ft_s(_ts.path_data, event.camera))
            _streak = _update_heading_streak(event.camera, _tid, _heading)
            if _heading == "approaching" and _streak >= 2:
                approaching_secure = True
            if _heading == "leaving" and _streak >= 2:
                leaving_scene = True
            if _speed == "running":
                moving_fast = True
            if _speed:
                speed_words.add(_speed)
            track_motion[_tid] = _build_motion(
                _ts.path_data, _ts.stationary, event.camera, speed_label=_speed,
            )
            _tp = _pos_by_track.get((event.camera, _tid))
            if _tp is not None:
                _d = ground.distance_to_secure_ft(
                    _tp.x, _tp.y, policy.get("secure_area"),
                    scale_ft=_scale, aspect_h_over_w=_aspect,
                )
                if _d is not None and (_raw_nearest is None or _d < _raw_nearest):
                    _raw_nearest = _d
        if _raw_nearest is not None:
            _key_tid = (event.track_ids or (event.event_id,))[0]
            nearest_ft = _stabilize_distance(event.camera, _key_tid, _raw_nearest)

        # Geometric-fusion logs (kept from the log-only phase) + the
        # cluster-mate index for dedup adoption below.
        for _tp in _positions:
            if _tp.camera == event.camera:
                logger.info(
                    "push: world pos camera=%s track=%s map=(%.3f, %.3f)",
                    _tp.camera, _tp.track_id, _tp.x, _tp.y,
                )
        for _cl in _clusters:
            if len(_cl.members) <= 1:
                continue
            logger.info(
                "push: geometric_dedup would_link=%s label=%s map=(%.3f, %.3f)",
                ",".join(f"{m.camera}/{m.track_id}" for m in _cl.members),
                _cl.label, _cl.x, _cl.y,
            )
            for _m in _cl.members:
                if _m.camera == event.camera:
                    geo_members[_m.track_id] = [
                        (o.camera, o.track_id)
                        for o in _cl.members if o.camera != event.camera
                    ]
    # A track approaching outranks another leaving in the same event.
    leaving_scene = leaving_scene and not approaching_secure

    snapshot, subject_kind, place_class = snapshot_from_review(
        event, zone_classes=policy["zone_classes"],
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded,
    )
    snapshot = replace(
        snapshot, approaching_secure=approaching_secure,
        leaving_scene=leaving_scene, moving_fast=moving_fast,
    )
    level = evaluate_ladder(snapshot)

    # Quiet hours: cap_quiet caps level at quiet (urgent exempt). SUPPRESSED
    # is not in ladder_policy.LEVELS -- a muted/suppressed snapshot has
    # nothing to cap, so it must skip this block rather than hit .index().
    qh_capped = False
    if qh_active and qh_mode == "cap_quiet" and level not in ("urgent", SUPPRESSED):
        from frigate_sidecar.push import ladder_policy
        quiet_idx = ladder_policy.LEVELS.index("quiet")
        level_idx = ladder_policy.LEVELS.index(level)
        if level_idx > quiet_idx:
            level = "quiet"
            qh_capped = True
    zone_name = event.zones[0] if event.zones else ""
    track_ids = event.track_ids or (event.event_id,)

    # Build reasons for decision trace.
    from frigate_sidecar.push import ladder_policy as _lp
    _zone_override_hit = bool(
        _lp.ZONE_OVERRIDES.get(snapshot.zone, {}).get(snapshot.subject)
    )
    trace_reasons: list[str] = []
    if _zone_override_hit:
        trace_reasons.append("zone_override")
    else:
        trace_reasons.append("routing_table")
    if qh_capped:
        trace_reasons.append("quiet_hours_cap")
    if approaching_secure:
        trace_reasons.append("approaching")
        if nearest_ft is not None and nearest_ft <= 100:
            trace_reasons.append(f"dist_{nearest_ft}ft")
    if leaving_scene:
        trace_reasons.append("leaving")
    if moving_fast:
        trace_reasons.append("running")

    mutated = 0
    for track_id in track_ids:
        card_key, existing, owning_camera, via_geo = _resolve_card_for_track(
            conn, camera=event.camera, track_id=track_id, subject_kind=subject_kind,
            zone_name=zone_name, zones=event.zones, now=now,
            geo_mates=geo_members.get(track_id),
            geo_enabled=bool(policy.get("geometric_dedup")),
        )
        card, mutation, sound = _advance_card(existing, level, card_key=card_key, now=now)
        if via_geo and "geo_dedup" not in trace_reasons:
            trace_reasons.append("geo_dedup")

        if mutation in (CREATE, ESCALATE, DEESCALATE):
            decision_trace.append(
                camera=event.camera,
                label=snapshot.label,
                subject=subject_kind,
                zones=list(event.zones) if event.zones else [],
                place=place_class,
                level=card.level,
                reasons=list(trace_reasons),
                event_id=event.event_id,
            )
        elif (
            mutation == SUPPRESSED_MUTATION
            and (existing is None or not existing.closed)
            and (snapshot.subject, snapshot.place) in _lp.OFF_CELLS
        ):
            # An `off` cell silenced this before evaluation -- trace it
            # anyway (level "off", once per track: later updates find the
            # closed row above and skip). Without this, Recent Decisions
            # shows nothing for exactly the cells the user silenced, so
            # there's no evidence trail to ever dial one back up. Global
            # mute also lands on SUPPRESSED but is excluded by the
            # OFF_CELLS check -- muting everything shouldn't flood the
            # trace.
            decision_trace.append(
                camera=event.camera,
                label=snapshot.label,
                subject=subject_kind,
                zones=list(event.zones) if event.zones else [],
                place=place_class,
                level="off",
                reasons=["routing_table", "suppressed"],
                event_id=event.event_id,
            )

        # RESOLVE copy shows the story duration (matches the LA's frozen
        # timer); live mutations show time-in-current-state.
        elapsed = max(
            0.0, now - (card.created_at if mutation == RESOLVE else card.state_since_at)
        )
        identity = event.sub_labels[0] if event.sub_labels else ""
        if not identity and engine is not None:
            identity = getattr(engine, "_sub_labels", {}).get(
                (event.camera, track_id), ""
            )
        # Notable verbs only (content design 2026-08-15): one story phrase
        # when something is actually happening, silence otherwise.
        if mutation == RESOLVE:
            story = f"left after {_fmt_elapsed(elapsed)}"
        elif approaching_secure:
            story = _approach_story(nearest_ft, speed_words)
        elif moving_fast:
            story = "moving fast"
        elif dwell_exceeded:
            story = "still there"
        elif leaving_scene:
            story = "leaving"
        else:
            story = ""
        primary, secondary = _copy(
            subject_kind, snapshot.label, owning_camera, zone_name,
            0.0 if mutation == RESOLVE else elapsed,
            identity=identity, story=story,
        )
        if owning_camera != event.camera:
            # A second camera is now contributing to a card it didn't
            # create -- surface that in the copy rather than silently
            # merging (docs "Cross-camera deduplication" §2). The card's
            # own `camera` field stays the original, first-seen camera
            # (below) so the app's timeline routing is unaffected.
            secondary = f"{secondary} · also on {event.camera.replace('_', ' ').title()}"

        # mute_sounds / quiet-hours mute_sounds mode: omit sound entirely
        # (urgent exempt from quiet-hours mute).
        if muted:
            sound = False
        if qh_active and qh_mode == "mute_sounds" and card.level != "urgent":
            sound = False

        payload = None
        warm_task = None
        media_handle = None
        media = None
        if should_push(card.level):
            media_handle, media, warm_task = _media_for(
                mutation, event, conn=conn, engine=engine, config=config,
            )

        # Live Activities go first so the card push below knows whether an
        # LA demonstrably covers this mutation (start accepted by APNs, or
        # an update landed on a confirmed per-activity token). Only then is
        # the card push demoted to a silent NC entry -- an LA that failed
        # anywhere along the way leaves the normal banner intact.
        la_only = bool(policy["live_activities"].get("la_only", False))
        # la_only's catch-all is quiet+ by its own contract -- decoupled from
        # should_push, which no longer includes quiet (2026-08-14).
        # A "glance" outcome cell is la_only applied per-cell: the merged
        # ladder promises a Live Activity with no banner, so the catch-all
        # guarantees an activity for *uncurated* content (a quiet person in
        # the yard matches no family today). Content a curated family
        # claims (openings, package, bins...) stays governed by that
        # family's toggle and picks -- those are the user's own curation.
        cell_glance = policy_settings.outcome_for(subject_kind, place_class) == "glance"
        if cell_glance and not la_only:
            native = live_activities.classify_family(
                subject_kind=subject_kind, label=snapshot.label,
                place_class=place_class, level=card.level,
            )
            if native is not None:
                cell_glance = False
        la_catch_all = (la_only or cell_glance) and card.level in ("quiet", "notify", "urgent")
        family = live_activities.should_start_activity(
            subject_kind=subject_kind, label=snapshot.label, place_class=place_class,
            level=card.level,
            families_enabled=policy["live_activities"],
            opening_picks=policy["live_activities"].get("opening_picks"),
            opening_ids=(zone_name, owning_camera) if zone_name else (owning_camera,),
            # Catch-all only for cards that would have pushed at all --
            # log-level noise shouldn't mint activities.
            catch_all=la_catch_all,
        )
        if family == live_activities.CATCH_ALL:
            # Diagnose *why* the catch-all fired: distinguish "no curated
            # family matches this card at all" (ordinary la_only behavior,
            # nothing to log) from "a curated family matched but wasn't
            # eligible" (family toggled off, or openings picks didn't
            # match) -- the case this fallback exists for.
            native_family = live_activities.classify_family(
                subject_kind=subject_kind, label=snapshot.label, place_class=place_class,
                level=card.level,
            )
            if native_family is not None:
                logger.info(
                    "la: family=%s not_eligible -> fallback family=%s (la_only)",
                    native_family, live_activities.CATCH_ALL,
                )
        # §8 instrument fields — derived from the engine's track store.
        la_state_since_ts = round(card.state_since_at, 1) if card.state_since_at else None
        la_motion: dict[str, Any] | None = None
        la_zones = None
        la_path = None
        if engine is not None:
            track_state = engine.tracks.get(event.camera, track_id)
            if track_state is not None:
                la_motion = track_motion.get(track_id)
                # Same rounded/hysteresis distance the alert copy used, so
                # the LA chip and the notification never disagree. Additive
                # optional field per the LA contract; ≤100 ft or absent.
                if la_motion is not None and nearest_ft is not None and nearest_ft <= 100:
                    la_motion = {**la_motion, "distance_ft": nearest_ft}
                live_zones = tuple(
                    z for z in track_state.first_seen_in_zone if z
                )
                la_zones = _build_zones_ladder(
                    event.zones, live_zones, policy["zone_classes"],
                )
                la_path = _build_la_path(track_state.path_data)

        la_covered = await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=family, camera=owning_camera, subject_kind=subject_kind,
            label=snapshot.label, primary=primary, secondary=secondary,
            elapsed_seconds=int(elapsed), media_handle=media_handle, now=now,
            sound_allowed=sound,
            state_since_ts=la_state_since_ts, motion=la_motion,
            zones=la_zones, path=la_path,
        )
        # la_first demotion: if the delivery mode is la_first and this is
        # NOT an escalation, broaden demotion to all la_capable devices that
        # have an open activity for this card — even if the LA update was
        # delta-suppressed this tick.
        delivery_mode = policy["live_activities"].get("delivery", "la_first")
        is_escalation = mutation == ESCALATE and card.level in ("notify", "urgent")
        demote_tokens: set[str] = set(la_covered)
        if delivery_mode == "la_first" and not la_only and not is_escalation:
            la_track_id = card_key.split(":", 2)[-1]
            for device in devices:
                if device.apns_token in demote_tokens:
                    continue
                if not device.la_capable:
                    continue
                row = store.find_activity(
                    conn, apns_token=device.apns_token,
                    situation_id=card_key, track_id=la_track_id,
                )
                if row is not None and not row["ended_at"]:
                    demote_tokens.add(device.apns_token)

        if demote_tokens:
            logger.info(
                "push: card push demoted to silent for %d device(s) — LA covers %s mutation=%s",
                len(demote_tokens), card_key, mutation,
            )

        if should_push(card.level):
            payload = build_card_payload(
                card, mutation, sound=sound and not la_only,
                subject_kind=subject_kind, place_class=place_class,
                label=snapshot.label, camera=owning_camera, zone_name=zone_name,
                glyph=_glyph_for(subject_kind, snapshot.label),
                primary=primary, secondary=secondary, event_ts=now, media=media,
                la_active=la_only, escalation_sound=escalation_sound,
            )

        await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=subject_kind, place_class=place_class,
            camera=owning_camera, zone_name=zone_name,
            labels=event.labels, zones=event.zones, now=now,
            demote_tokens=demote_tokens,
            suppress_demoted=delivery_mode == "la_first" and not la_only,
        )
        if warm_task is not None:
            # Runs concurrently with the sends above, not in series (plan §4
            # lever 4's rule, reused here): the push already carries the
            # `media` URL optimistically, so a slow or failed Frigate fetch
            # costs the notification its image, never its existence.
            with contextlib.suppress(Exception):
                await warm_task
        conn.commit()
        mutated += 1

    return mutated


def resound_payload_for(card, context: dict[str, str]) -> dict:
    """`delivery.sweep_urgent_resound`'s `payload_for_resound` callback:
    rebuilds the same copy the card's context implies, sound forced on.
    Same shape as a live `escalate`, since a re-sound is exactly that from
    the app's point of view -- the card doesn't change level, but it does
    buzz again."""
    now = time.time()
    elapsed = max(0.0, now - card.state_since_at)
    subject_kind = context.get("subject_kind") or ""
    primary, secondary = _copy(
        subject_kind, "", context.get("camera", ""), context.get("zone_name", ""), elapsed,
        story="still there",
    )
    active = policy_settings.get_active()
    la_only = bool(active.get("live_activities", {}).get("la_only", False))
    return build_card_payload(
        card, "escalate", sound=not la_only, la_active=la_only,
        subject_kind=subject_kind, place_class=context.get("place_class", ""),
        label=context.get("label", ""), camera=context.get("camera", ""),
        zone_name=context.get("zone_name", ""),
        glyph=_glyph_for(subject_kind, ""), primary=primary, secondary=secondary, event_ts=now,
        escalation_sound=active.get("escalation_sound", "urgent"),
    )


async def handle_delivery_resolve(
    camera: str,
    track_id: str,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    zone_name: str = "",
    subject_kind: str = "",
    now: float | None = None,
) -> int:
    """A tracked object ended (`frigate/events` `msg_type == "end"`) -- the
    faster, authoritative resolution signal `advance_card`'s `resolved=True`
    needs (see `cards.classify_mutation`'s docstring on why a ladder level
    alone can't say this)."""
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now

    # This track was merged onto another camera's card (cross-camera dedup,
    # docs "Cross-camera deduplication") -- it was never the card's identity,
    # just one contributor among possibly several. Only the *original*
    # camera resolving (below, its own natural key) ends the card, even if
    # this one lingers a beat behind: dropping the alias quietly is correct
    # whether the owning camera is still tracking (this contributor just
    # stops enriching) or has already resolved too (the card is already
    # closing on its own path, and this track will get a fresh card of its
    # own if it's still detected afterward -- it will have moved zones by
    # then in the walk-through case that motivated dedup in the first place).
    _heading_streaks.pop((camera, track_id), None)
    # Same lifetime as the streaks: without this the announced-distance map
    # leaks one entry per track ever seen (nothing else deletes from it).
    _announced_distance.pop((camera, track_id), None)
    if card_store.get_track_alias(conn, camera, track_id) is not None:
        card_store.delete_track_alias(conn, camera, track_id)
        return 0

    # subject_kind isn't known from the object stream alone (no labels are
    # guaranteed to match what the review classified it as); resolve every
    # card keyed on this track id under any subject kind class this MVP uses.
    candidates = (
        [subject_kind] if subject_kind else list(_SUBJECT_GLYPH.keys())
    )
    resolved = 0
    for kind in candidates:
        card_key = build_card_key(camera=camera, subject_kind=kind, subject_id=track_id)
        existing = card_store.get_card(conn, card_key)
        if existing is None or existing.closed:
            continue
        card, mutation, sound = _advance_card(
            existing, existing.level, card_key=card_key, now=now, resolved=True,
        )
        elapsed = max(
            0.0, now - (card.created_at if mutation == RESOLVE else card.state_since_at)
        )
        story = f"left after {_fmt_elapsed(elapsed)}" if mutation == RESOLVE else ""
        primary, secondary = _copy(
            kind, "", camera, zone_name, 0.0 if mutation == RESOLVE else elapsed,
            story=story,
        )
        # End the LA first; if the end landed on a confirmed activity, the
        # resolve card push goes out quiet (NC text only, no banner).
        la_covered = await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=None, camera=camera, subject_kind=kind, label="",
            primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
            media_handle=None, now=now, sound_allowed=sound,
            state_since_ts=round(card.state_since_at, 1) if card.state_since_at else None,
        )
        policy = policy_settings.get_active()
        la_only = bool(policy.get("live_activities", {}).get("la_only", False))
        demote_resolve: set[str] = set(la_covered)
        delivery_mode = policy.get("live_activities", {}).get("delivery", "la_first")
        if delivery_mode == "la_first" and not la_only:
            la_tid = card_key.split(":", 2)[-1]
            for device in devices:
                if device.apns_token in demote_resolve or not device.la_capable:
                    continue
                row = store.find_activity(
                    conn, apns_token=device.apns_token,
                    situation_id=card_key, track_id=la_tid,
                )
                if row is not None and not row["ended_at"]:
                    demote_resolve.add(device.apns_token)
        payload = None
        if should_push(card.level):
            payload = build_card_payload(
                card, mutation, sound=sound and not la_only, subject_kind=kind,
                place_class="", camera=camera, zone_name=zone_name,
                glyph=_glyph_for(kind, ""),
                primary=primary, secondary=secondary, event_ts=now,
                la_active=la_only,
                escalation_sound=policy.get("escalation_sound", "urgent"),
            )
        await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=kind, camera=camera, zone_name=zone_name,
            now=now, demote_tokens=demote_resolve,
            suppress_demoted=delivery_mode == "la_first" and not la_only,
        )
        conn.commit()
        resolved += 1
    return resolved


def _relaxed_level(current_level: str, mode: str) -> str | None:
    """Compute the target level for a recognition relaxation. Returns the
    new level, or ``None`` if no change."""
    from frigate_sidecar.push import ladder_policy

    levels = ladder_policy.LEVELS
    idx = levels.index(current_level)
    if mode == "relax_one":
        target = max(0, idx - 1)
    elif mode == "relax_to_quiet":
        target = min(idx, levels.index("quiet"))
    else:
        return None
    return levels[target] if target < idx else None


async def handle_zone_transition(
    camera: str,
    track_id: str,
    current_zones: tuple[str, ...],
    *,
    label: str,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    engine: PushEngine | None = None,
    now: float | None = None,
) -> int:
    """A tracked object's zone set changed (`frigate/events`).

    Reviews own story *existence*; this hook exists because Frigate's review
    items go quiet on stationary objects — a person who loiters and drifts
    into hotter ground (driveway → charger) never gets a review update, so
    the review's frozen zone list kept a Restricted-zone loiter routed
    Semi-private (observed live 2026-08-14, person at 0.92 in `charger` for
    minutes with no escalation). The event stream knew within seconds.

    Escalation-only, by design: if the hottest current zone routes ABOVE the
    card's level, synthesize a review-shaped update through
    `handle_delivery_event` (which owns escalation, LA late-start, demotion,
    and sounds). Routing DOWN stays review-authoritative — an object stepping
    briefly onto cooler ground must not deescalate a story the review still
    considers hot.

    No card (alias or direct) → no-op: this hook never *creates* stories.
    """
    if not config.delivery_enabled or not current_zones:
        return 0
    now = time.time() if now is None else now

    probe = ReviewEvent(
        review_id=f"zone-transition:{camera}:{track_id}", camera=camera,
        severity="alert", labels=(label,) if label else (),
        track_ids=(track_id,), zones=tuple(current_zones),
    )
    subject_kind = classify_subject(probe)

    card_key = card_store.get_track_alias(conn, camera, track_id)
    if card_key is None:
        card_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    card = card_store.get_card(conn, card_key)
    if card is None or card.closed or card.resolved:
        return 0
    from frigate_sidecar.push import ladder_policy
    if card.level not in ladder_policy.LEVELS:
        return 0

    policy = policy_settings.get_active()
    zone_classes = policy["zone_classes"]

    # Rank each current zone by what it would route for this subject —
    # per-zone Snapshot so zone overrides apply, same authority as the
    # review path.
    best_zone: str | None = None
    best_idx = -1
    for zone in current_zones:
        place = zone_classes.get(zone) or policy_settings.guess_zone_class(zone)
        level = evaluate_ladder(Snapshot(
            subject=subject_kind, place=place, zone=zone,
            label=label, nobody_home=False, night=False,
            dwell_exceeded=False, muted=False,
        ))
        if level in ladder_policy.LEVELS:
            idx = ladder_policy.LEVELS.index(level)
            if idx > best_idx:
                best_idx = idx
                best_zone = zone

    if best_zone is None or best_idx <= ladder_policy.LEVELS.index(card.level):
        return 0

    logger.info(
        "push: zone transition escalates card=%s zone=%s (%s -> %s)",
        card.card_key, best_zone, card.level, ladder_policy.LEVELS[best_idx],
    )
    ordered = (best_zone, *(z for z in current_zones if z != best_zone))
    synthetic = ReviewEvent(
        review_id=f"zone-transition:{camera}:{track_id}", camera=camera,
        severity="alert", labels=(label,) if label else (),
        track_ids=(track_id,), zones=ordered,
    )
    return await handle_delivery_event(
        synthetic, conn=conn, devices=devices, transport=transport,
        config=config, engine=engine, now=now,
    )


async def handle_recognition_event(
    camera: str,
    track_id: str,
    sub_label: str,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    label: str = "person",
    now: float | None = None,
) -> int:
    """A face sub_label or plate landed on a tracked object — check
    recognition settings and emit a silent deescalate if applicable.

    Identity-driven mutations are always silent: no sound, no LA alert,
    regardless of mute settings (design brief: "watching the instrument
    calm itself down is the feature")."""
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now
    policy = policy_settings.get_active()
    recognition = policy.get("recognition", {})

    if label == "person" or label in _ANIMAL_LABELS:
        mode = recognition.get("known_person", "off")
        subject_kind = "person"
    elif label in _VEHICLE_LABELS:
        mode = recognition.get("known_vehicle", "off")
        subject_kind = "vehicle"
    else:
        return 0

    if mode == "off" or not sub_label:
        return 0

    card_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    existing = card_store.get_card(conn, card_key)
    if existing is None or existing.closed or existing.resolved:
        return 0

    target_level = _relaxed_level(existing.level, mode)
    if target_level is None:
        return 0

    card, mutation, _sound = _advance_card(
        existing, target_level, card_key=card_key, now=now,
    )
    if mutation != DEESCALATE:
        return 0

    context = card_store.get_card_context(conn, card_key) or {}

    decision_trace.append(
        camera=camera,
        label=label,
        subject=subject_kind,
        zones=[],
        place=context.get("place_class", ""),
        level=card.level,
        reasons=["recognition_relax"],
        event_id=f"{camera}:{track_id}",
    )
    zone_name = context.get("zone_name", "")
    place_class = context.get("place_class", "")
    owning_camera = context.get("camera", camera)
    elapsed = max(0.0, now - card.state_since_at)
    primary, secondary = _copy(
        subject_kind, label, owning_camera, zone_name, elapsed, identity=sub_label,
    )

    la_covered = await _deliver_live_activities(
        conn, devices, transport, config=config, card=card, mutation=mutation,
        family=None, camera=owning_camera, subject_kind=subject_kind, label=label,
        primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
        media_handle=None, now=now, sound_allowed=False,
        state_since_ts=round(card.state_since_at, 1) if card.state_since_at else None,
    )
    demote_recog: set[str] = set(la_covered)
    recog_policy = policy_settings.get_active()
    recog_la_only = bool(recog_policy.get("live_activities", {}).get("la_only", False))
    recog_delivery = recog_policy.get("live_activities", {}).get("delivery", "la_first")
    if recog_delivery == "la_first" and not recog_la_only:
        la_tid = card_key.split(":", 2)[-1]
        for device in devices:
            if device.apns_token in demote_recog or not device.la_capable:
                continue
            row = store.find_activity(
                conn, apns_token=device.apns_token,
                situation_id=card_key, track_id=la_tid,
            )
            if row is not None and not row["ended_at"]:
                demote_recog.add(device.apns_token)
    payload = None
    if should_push(card.level):
        payload = build_card_payload(
            card, mutation, sound=False, subject_kind=subject_kind,
            place_class=place_class, label=label, camera=owning_camera,
            zone_name=zone_name, glyph=_glyph_for(subject_kind, label),
            primary=primary, secondary=secondary, event_ts=now,
            la_active=True,
            escalation_sound=recog_policy.get("escalation_sound", "urgent"),
        )
    _recog_la = recog_policy.get("live_activities", {})
    await send_card_mutation(
        conn, transport, devices, card, mutation, payload,
        subject_kind=subject_kind, place_class=place_class,
        camera=owning_camera, zone_name=zone_name, now=now,
        demote_tokens=demote_recog,
        suppress_demoted=_recog_la.get("delivery", "la_first") == "la_first"
        and not _recog_la.get("la_only", False),
    )
    conn.commit()
    logger.info(
        "recognition: %s on %s:%s -> deescalate %s->%s (mode=%s)",
        sub_label, camera, track_id, existing.level, target_level, mode,
    )
    return 1
