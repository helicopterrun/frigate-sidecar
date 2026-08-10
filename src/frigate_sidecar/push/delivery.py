"""The delivery pipeline (Elsinore Phase 2): wraps the ladder evaluator with
card state, mutation semantics, and APNs payload construction.

`ladder.evaluate_ladder` stays a pure function with no notion of "the same
subject seen five times" -- this module is what turns a *stream* of
evaluations into a card that mutates in place and a bounded number of
sounds. Three layers, cleanest to unit-test in isolation:

1. **State transition** (`advance_card`) -- pure, no I/O: given the card
   store's current row for a key (or `None`) and a new ladder level, produce
   the next `Card` and the mutation kind (`cards.CREATE` etc.), per the
   mutation-classification table and the sound-accounting policy in
   `cards.py`.
2. **Payload construction** (`build_card_payload`) -- pure: the level->APNs
   mapping plus the versioned wire contract (`docs/apns-payload-spec.md`).
3. **Orchestration** (`send_card_mutation`, `sweep_urgent_resound`) --
   persists the card via `card_store` and sends through the sidecar's
   existing `PushTransport.send_situation` (no new transport invented; see
   `transport.py`). This is the only layer that touches the DB or the
   network, and it is a thin wrapper around 1 and 2.

Still no Live Activity code (Phase 3) -- every push here is an ordinary
alert or silent push, `mutable-content` set so the NSE can still attach a
thumbnail from `media`, but nothing here starts, updates, or ends an
Activity.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from frigate_sidecar.push import card_store
from frigate_sidecar.push.cards import (
    CREATE,
    DEESCALATE,
    ENRICH,
    ESCALATE,
    RESOLVE,
    SUPPRESSED,
    Card,
    _level_index,
    classify_mutation,
    should_sound,
    urgent_resound_due,
)
from frigate_sidecar.push.models import Device
from frigate_sidecar.push.payload import APNS_MAX_PAYLOAD_BYTES, payload_size
from frigate_sidecar.push.transport import PushTransport

logger = logging.getLogger(__name__)

#: Payload contract version (`docs/apns-payload-spec.md`). Bump on any
#: breaking change to the fields below; the app pins against this.
CONTRACT_VERSION = 1

#: Level -> APNs mapping (design doc §4). `log` and `suppressed` never push;
#: everything else does, at the given `interruption-level`, with sound
#: decided separately by `cards.should_sound` / the urgent re-sound timer --
#: this table only says *whether a level can ever carry sound*, matching
#: `cards.SOUNDED_LEVELS`.
LEVEL_APNS: dict[str, dict[str, Any]] = {
    "urgent": {"push": True, "interruption_level": "time-sensitive"},
    "notify": {"push": True, "interruption_level": "active"},
    "quiet": {"push": True, "interruption_level": "passive"},
    "log": {"push": False, "interruption_level": None},
    SUPPRESSED: {"push": False, "interruption_level": None},
}


def should_push(level: str) -> bool:
    return LEVEL_APNS.get(level, {}).get("push", False)


def sound_name_for_card(level: str, subject_kind: str = "", label: str = "") -> str:
    """Per-family/level sound name from the app's .caf catalog."""
    if level == "urgent":
        return "urgent.caf"
    if subject_kind in ("stranger", "known") and label == "person":
        return "at-the-door.caf"
    if label == "package":
        return "package-delivery.caf"
    return "general.caf"


def build_card_key(
    *, camera: str, subject_kind: str, subject_id: str, source: str = "detection"
) -> str:
    """The card key -- stable per ongoing subject, and also the APNs
    `apns-collapse-id`.

    **Zone is deliberately not part of identity.** An earlier version keyed
    on `{camera}:{zone}:{subject_kind}:{subject_id}`, matching the design
    brief's literal example -- but a tracked object that gains or changes
    zone mid-lifetime (a car first seen with no zone, then entering
    `parking_spot`) got a *new* card instead of mutating its existing one,
    observed live on the first supervised run
    (`alley-wide:_:thing:...-m0d7oe` then `alley-wide:parking_spot:thing:...
    -m0d7oe`, same track). That breaks the one-card-per-subject invariant
    the whole pipeline exists to provide. Zone is still carried on every
    payload (`zone_name`) and still drives mutation classification when it
    changes the routed level -- it's context, not identity.

    `subject_id` must survive Frigate re-detections of the *same* subject:
    for a tracked object that is the Frigate `track_id` (stable for the
    object's lifetime, per `situations.py`'s existing use of it); for an
    opening (garage door, gate) it is the opening/zone's configured id, since
    those have no track id at all. A system card (`source == "system"`) has
    no subject or place -- it is keyed on camera + a fixed reason instead
    (e.g. `"front:system:offline"`), so two different system conditions on
    the same camera never collide.

    The key is opaque to the app except for one documented rule: the camera
    is always the first `:`-separated component.
    """
    if source == "system":
        return f"{camera}:system:{subject_id}"
    return f"{camera}:{subject_kind}:{subject_id}"


def advance_card(
    existing: Card | None, new_level: str, *, card_key: str, now: float, resolved: bool = False,
) -> tuple[Card, str, bool]:
    """The pure state transition at the heart of the pipeline.

    Returns `(new_card, mutation, sound)`. `new_card` is always persisted by
    the caller, even for `SUPPRESSED` (as a closed row, so the *next*
    evaluation for this key -- unmuted -- correctly reads as a fresh
    `create` rather than an enrich of a card that no longer exists
    conceptually). Sound accounting (`cards.should_sound`) is applied here,
    against the card *before* this mutation's sound is added to its budget.
    """
    mutation = classify_mutation(existing, new_level, resolved=resolved)

    if mutation == SUPPRESSED:
        base = existing if existing is not None else Card(
            card_key=card_key, level=new_level, created_at=now, updated_at=now, state_since_at=now,
        )
        return (
            replace(base, level=new_level, updated_at=now, resolved=True, closed=True),
            mutation,
            False,
        )

    if mutation == RESOLVE:
        # Level is unchanged -- "resolved" reports how long the *last* state
        # held, not whatever level this call happened to re-evaluate with.
        base = existing if existing is not None else Card(
            card_key=card_key, level=new_level, created_at=now, updated_at=now, state_since_at=now,
        )
        return replace(base, updated_at=now, resolved=True, closed=True), mutation, False

    if mutation == CREATE:
        card = Card(
            card_key=card_key, level=new_level, peak_level=new_level,
            created_at=now, updated_at=now, state_since_at=now,
        )
    else:
        assert existing is not None
        state_since = now if mutation in (ESCALATE, DEESCALATE) else existing.state_since_at
        peak = existing.peak_level
        if _level_index(new_level) > _level_index(peak):
            peak = new_level
        card = replace(
            existing, level=new_level, peak_level=peak,
            updated_at=now, state_since_at=state_since,
        )

    sound = should_sound(card, mutation, new_level)
    if sound:
        card.sound_count += 1
        card.last_sound_at = now

    return card, mutation, sound


def apply_urgent_resound(card: Card, *, now: float) -> Card:
    """Spend the one urgent-only re-sound. Caller has already confirmed
    `cards.urgent_resound_due`; this just books it."""
    return replace(card, resound_count=card.resound_count + 1, last_sound_at=now, updated_at=now)


def build_card_payload(
    card: Card,
    mutation: str,
    *,
    sound: bool,
    subject_kind: str,
    place_class: str,
    label: str = "",
    camera: str,
    zone_name: str,
    glyph: str,
    primary: str,
    secondary: str,
    event_ts: float,
    media: str | None = None,
    deep_link: str | None = None,
    la_active: bool = False,
) -> dict[str, Any]:
    """The full APNs body for one card mutation (`docs/apns-payload-spec.md`).

    `interruption-level` comes from the *card's* level, not the mutation --
    a silent deescalate to `quiet` still carries `passive`, since that is
    what the level->APNs table says about the level the card is at now, and
    the app's local notification-center presentation depends on it even when
    there is no sound. `sound` is the one field the caller (which alone
    knows the sound-accounting outcome) must supply rather than derive here.
    """
    interruption_level = LEVEL_APNS.get(card.level, {}).get("interruption_level")
    # Silent enriches: same level, new facts — no new banner/sound (§2).
    if mutation == ENRICH:
        interruption_level = "passive"
        sound = False
    # §2: LA is the alerting surface — card push demoted to silent NC update.
    if la_active and mutation != CREATE:
        interruption_level = "passive"
        sound = False
    aps: dict[str, Any] = {
        "alert": {"title": primary, "body": secondary},
        "mutable-content": 1,
    }
    if interruption_level is not None:
        aps["interruption-level"] = interruption_level
    if sound:
        aps["sound"] = sound_name_for_card(card.level, subject_kind, label)
    aps["thread-id"] = camera
    aps["category"] = f"card.{card.level}"

    state_since_ts = round(card.state_since_at, 3)
    payload: dict[str, Any] = {
        "aps": aps,
        "v": CONTRACT_VERSION,
        "card_key": card.card_key,
        "mutation": mutation,
        "level": card.level,
        "subject_kind": subject_kind,
        "place_class": place_class,
        "camera": camera,
        "zone_name": zone_name,
        "glyph": glyph,
        "primary": primary,
        "secondary": secondary,
        "event_ts": round(event_ts, 3),
        "state_since_ts": state_since_ts,
    }
    if media:
        payload["media"] = media
    if deep_link:
        # Additive, optional -- no `v` bump. `?t=<state_since_ts>` (same
        # float formatting as the other timestamp fields) lets the app open
        # the camera timeline parked at the moment the card's current state
        # became true, instead of falling back to the review feed.
        payload["deep_link"] = f"{deep_link}?t={state_since_ts}"
    return _fit_to_budget(payload)


def _fit_to_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Same trim rule as `payload.py`'s `_fit_to_budget`: a user-authored
    copy string is the one unbounded field, and an over-budget push is
    dropped outright by Apple rather than trimmed."""
    if payload_size(payload) <= APNS_MAX_PAYLOAD_BYTES:
        return payload
    payload["primary"] = str(payload["primary"])[:120]
    payload["secondary"] = str(payload["secondary"])[:180]
    payload["aps"]["alert"]["title"] = payload["primary"]
    payload["aps"]["alert"]["body"] = payload["secondary"]
    if payload_size(payload) > APNS_MAX_PAYLOAD_BYTES:  # pragma: no cover - unreachable today
        logger.warning("push: card payload still over %d bytes after trim", APNS_MAX_PAYLOAD_BYTES)
    return payload


_LEVEL_TO_SEVERITY = {"urgent": "alert", "notify": "alert", "quiet": "detection", "log": "log"}
_SEVERITY_INDEX = {"log": 0, "detection": 1, "alert": 2}

_SOUND_RATE_KEY = "_card_sound"


def _device_eligible(
    device: Device, *, camera: str, labels: tuple[str, ...], card_level: str,
) -> bool:
    if device.cameras and camera not in device.cameras:
        return False
    if device.labels and not (set(device.labels) & set(labels)):
        return False
    card_sev = _LEVEL_TO_SEVERITY.get(card_level, "log")
    if _SEVERITY_INDEX.get(card_sev, 0) < _SEVERITY_INDEX.get(device.min_severity, 2):
        return False
    return True


def _is_snoozed(conn: Any, device: Device, camera: str, *, now: float) -> bool:
    from frigate_sidecar.push import store
    snoozed = store.active_snoozes(conn, device.apns_token, now=now)
    return "global" in snoozed or f"camera:{camera}" in snoozed


async def send_card_mutation(
    conn: Any,
    transport: PushTransport,
    devices: list[Device],
    card: Card,
    mutation: str,
    payload: dict[str, Any] | None,
    *,
    subject_kind: str = "",
    place_class: str = "",
    camera: str = "",
    zone_name: str = "",
    labels: tuple[str, ...] = (),
    now: float | None = None,
) -> int:
    """Persist `card` and send to eligible devices, honoring per-device
    filtering, snooze, quiet resolves, and the global sounding rate cap."""
    import time as _time
    from frigate_sidecar.push import store

    now = _time.time() if now is None else now
    card_store.upsert_card(
        conn, card, subject_kind=subject_kind, place_class=place_class,
        camera=camera, zone_name=zone_name,
    )
    if payload is None:
        return 0

    # Quiet resolves: don't push resolve for cards whose peak never exceeded quiet.
    if mutation == RESOLVE:
        from frigate_sidecar.push import ladder_policy
        quiet_idx = ladder_policy.LEVELS.index("quiet")
        peak_idx = _level_index(card.peak_level)
        if peak_idx <= quiet_idx:
            return 0
        # Cards that did produce a notification get one final silent update.
        payload = dict(payload)
        payload["aps"] = dict(payload["aps"])
        payload["aps"].pop("sound", None)
        payload["aps"]["interruption-level"] = "passive"

    has_sound = bool(payload.get("aps", {}).get("sound"))

    sent = 0
    for device in devices:
        if not _device_eligible(device, camera=camera, labels=labels, card_level=card.level):
            continue
        if _is_snoozed(conn, device, camera, now=now):
            continue

        dev_payload = payload
        if has_sound:
            recent = store.count_sends_since(
                conn, apns_token=device.apns_token, situation_id=_SOUND_RATE_KEY,
                since=now - 3600.0,
            )
            if recent >= 10:
                dev_payload = dict(payload)
                dev_payload["aps"] = dict(dev_payload["aps"])
                dev_payload["aps"].pop("sound", None)
                dev_payload["aps"]["interruption-level"] = "passive"
                store.bump_suppressed(
                    conn, apns_token=device.apns_token, situation_id=_SOUND_RATE_KEY,
                )
                conn.commit()
            else:
                suppressed = store.take_suppressed(
                    conn, apns_token=device.apns_token, situation_id=_SOUND_RATE_KEY,
                )
                conn.commit()
                if suppressed:
                    dev_payload = dict(payload)
                    dev_payload["aps"] = dict(dev_payload["aps"])
                    body = dev_payload["aps"].get("alert", {}).get("body", "")
                    dev_payload["aps"]["alert"] = dict(dev_payload["aps"].get("alert", {}))
                    dev_payload["aps"]["alert"]["body"] = f"{body} · +{suppressed} more"
                store.record_send(
                    conn, apns_token=device.apns_token, situation_id=_SOUND_RATE_KEY, now=now,
                )
                conn.commit()

        priority = 10 if bool(dev_payload.get("aps", {}).get("sound")) else 5
        result = await transport.send_situation(
            device, payload=dev_payload, collapse_id=card.card_key,
            apns_priority=priority,
        )
        if not result.ok:
            logger.info(
                "push: card send failed device=%s card_key=%s mutation=%s error=%s",
                device.device_id, card.card_key, mutation, result.error,
            )
        sent += 1
    logger.info(
        "push: card mutation=%s level=%s card_key=%s sound=%s devices=%d",
        mutation, card.level, card.card_key, has_sound, sent,
    )
    return sent


async def sweep_urgent_resound(
    conn: Any,
    transport: PushTransport,
    devices: list[Device],
    *,
    now: float | None = None,
    interval_s: float = 120.0,
    enabled: bool = True,
    max_resounds: int = 5,
    payload_for_resound: Any = None,
) -> int:
    """Check every open `urgent` card for repeating re-sounds (§2). Each
    re-sound counts against the global sounding rate cap."""
    if not enabled or payload_for_resound is None:
        return 0
    now = time.time() if now is None else now
    resounded = 0
    for card, context in card_store.list_open_urgent_cards(conn):
        if not urgent_resound_due(
            card, now=now, interval_s=interval_s, enabled=enabled, max_resounds=max_resounds,
        ):
            continue
        card = apply_urgent_resound(card, now=now)
        payload = payload_for_resound(card, context)
        await send_card_mutation(
            conn, transport, devices, card, ESCALATE, payload, now=now, **context,
        )
        resounded += 1
    return resounded
