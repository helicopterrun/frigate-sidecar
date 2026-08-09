"""Wires the delivery pipeline (`delivery.py`, `cards.py`) to the sidecar's
existing Frigate event flow (Elsinore Phase 2, config-gated, default off).

No new transport, no new MQTT subscription: `PushEngine.handle_event` /
`handle_object_payload` are already the two entry points every
`frigate/reviews` and `frigate/events` message passes through, so this
module is a plain function each of them calls once, guarded by
`settings.push.delivery_enabled`.

**Subject/place classification here is a deliberate MVP**, not the
full-fidelity mapping the design doc's ladder deserves. `frigate/reviews`
carries labels and a cumulative zone list but no resolved identity (Phase
5's territory) and no per-camera place-class config beyond
`delivery_zone_place_map` -- so `classify_subject`/`classify_place` are
heuristics, documented as such, safe to ship because the whole pipeline is
off by default. Tightening them is a data/config change here, not a change
to `ladder.py` or `delivery.py`, exactly like `ladder_policy.py`'s own
separation of policy from evaluation order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING

from frigate_sidecar.push import card_store, live_activities, store
from frigate_sidecar.push.cards import CREATE, ENRICH, RESOLVE, Card
from frigate_sidecar.push.delivery import advance_card as _advance_card
from frigate_sidecar.push.delivery import (
    build_card_key,
    build_card_payload,
    send_card_mutation,
    should_push,
)
from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder
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

logger = logging.getLogger(__name__)

#: Frigate labels this MVP treats as an animal subject, beyond the
#: dangerous-animal labels `ladder.py` already reclassifies via `label`
#: regardless of what subject is passed in.
_ANIMAL_LABELS = frozenset({"dog", "cat", "bird", "deer", "squirrel", "raccoon", "bear", "skunk"})

_SUBJECT_GLYPH = {
    "stranger": "person.stranger",
    "known": "person.identified",
    "animal": "animal.seen",
    "thing": "thing.detected",
}

_SUBJECT_COPY = {
    "stranger": "Person",
    "known": "Person",
    "animal": "Animal",
}


def classify_subject(event: ReviewEvent) -> str:
    """Best-effort subject classification off `frigate/reviews` alone.
    `sub_labels` (Phase 5) is the only signal available yet for a resolved
    identity; its presence is read as "known", its absence as "stranger" for
    a person label -- never the reverse, so copy never claims an identity
    that hasn't resolved (design doc §5)."""
    labels = set(event.labels)
    if "person" in labels:
        return "known" if event.sub_labels else "stranger"
    if labels & _ANIMAL_LABELS:
        return "animal"
    return "thing"


def classify_place(event: ReviewEvent, zone_place_map: dict[str, str]) -> str:
    for zone in event.zones:
        if zone in zone_place_map:
            return zone_place_map[zone]
    return "yard" if event.zones else "street"


def snapshot_from_review(
    event: ReviewEvent,
    *,
    zone_place_map: dict[str, str],
    nobody_home: bool = False,
    night: bool = False,
    dwell_exceeded: bool = False,
    muted: bool = False,
) -> tuple[Snapshot, str, str]:
    """Build the ladder `Snapshot`, returning `(snapshot, subject_kind,
    place_class)` since the payload contract needs the classification
    alongside the level the snapshot evaluates to."""
    subject_kind = classify_subject(event)
    place_class = classify_place(event, zone_place_map)
    label = event.labels[0] if event.labels else ""
    snapshot = Snapshot(
        subject=subject_kind, place=place_class, label=label,
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded, muted=muted,
    )
    return snapshot, subject_kind, place_class


def _copy(
    subject_kind: str, label: str, camera: str, zone_name: str, elapsed_s: float,
) -> tuple[str, str]:
    subject_text = _SUBJECT_COPY.get(subject_kind) or pretty_label(label) or "Motion"
    place_text = zone_name or camera
    primary = f"{subject_text} at {place_text.replace('_', ' ').title()}"
    secondary = f"{place_text.replace('_', ' ').title()} · {int(elapsed_s)}s"
    return primary, secondary


def _glyph_for(subject_kind: str, label: str) -> str:
    if label:
        return f"{subject_kind}.{label}"
    return _SUBJECT_GLYPH.get(subject_kind, "motion.detected")


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
    now: float,
) -> tuple[str, Card | None, str]:
    """Which card this (camera, track_id) evaluation belongs to, applying
    cross-camera dedup (docs/push-notifications.md "Cross-camera
    deduplication") before a fresh card key would otherwise be minted.

    Returns `(card_key, existing_card_or_None, owning_camera)`.
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
    3. Otherwise (existing card under its own key, or no zone to dedup on)
       -- this track's own natural key, unchanged from before this feature.
    """
    alias_key = card_store.get_track_alias(conn, camera, track_id)
    if alias_key is not None:
        aliased = card_store.get_card(conn, alias_key)
        if aliased is not None and not aliased.closed:
            ctx = card_store.get_card_context(conn, alias_key)
            return alias_key, aliased, (ctx or {}).get("camera") or camera
        card_store.delete_track_alias(conn, camera, track_id)

    natural_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    existing = card_store.get_card(conn, natural_key)
    if existing is None and zone_name:
        candidate_key = card_store.find_dedup_candidate(
            conn, subject_kind=subject_kind, zone_name=zone_name,
            exclude_key=natural_key, now=now, window_s=_DEDUP_WINDOW_S,
        )
        if candidate_key is not None:
            candidate = card_store.get_card(conn, candidate_key)
            if candidate is not None and not candidate.closed:
                card_store.set_track_alias(conn, camera, track_id, candidate_key, now)
                ctx = card_store.get_card_context(conn, candidate_key)
                return candidate_key, candidate, (ctx or {}).get("camera") or camera

    return natural_key, existing, camera


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
) -> None:
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
    """
    if not config.delivery_la_enabled:
        return
    card_key = card.card_key
    # The primary/original subject id, stable across cross-camera dedup --
    # `card_key` is always `{camera}:{subject_kind}:{subject_id}`, so this is
    # the same value regardless of which camera's event triggered this call.
    la_track_id = card_key.split(":", 2)[-1]

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
            )
            payload = live_activities.build_la_end_payload(content_state=content_state, now=now)
            # The per-activity token if the app ever uploaded one, else the
            # push-to-start token: APNs accepts an `end` push-to-start too, so
            # a card resolving before the token arrives still dismisses the
            # activity instead of leaving it stuck open forever.
            token = row["token"] or device.push_to_start_token
            if token:
                await transport.send_live_activity(
                    device, token=token, payload=payload, collapse_id=card_key, event="end",
                )
            store.close_activity(conn, row["activity_id"], now=now)
            continue

        if row is None:
            if mutation != CREATE or family is None or not device.push_to_start_token:
                continue
            content_state = live_activities.build_content_state(
                level=card.level, mutation=mutation,
                glyph=live_activities.glyph_for(
                    family, subject_kind=subject_kind, label=label, mutation=mutation,
                ),
                primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
                card_key=card_key, thumbnail_handle=media_handle, thumbnail_revision=1,
            )
            payload = live_activities.build_la_start_payload(
                content_state=content_state, family=family, camera=camera,
                track_id=la_track_id, card_key=card_key, now=now,
            )
            result = await transport.send_live_activity(
                device, token=device.push_to_start_token, payload=payload,
                collapse_id=card_key, event="start",
            )
            if not result.ok:
                continue
            activity_id = f"a_{secrets.token_urlsafe(8)}"
            store.open_activity(
                conn, activity_id=activity_id, apns_token=device.apns_token,
                situation_id=card_key, track_id=la_track_id, camera=camera,
                collapse_id=card_key, handle=media_handle or "", now=now,
            )
            store.record_activity_send(conn, activity_id=activity_id, now=now)
            continue

        # An activity is already running for this card+device -- advance it,
        # if the app has uploaded the per-activity token yet. "Not yet" is a
        # normal transient state (design doc §7): the activity is on screen
        # and will catch up on the next mutation, so this update is simply
        # dropped rather than buffered or retried.
        if not row["token"]:
            continue
        revision = int(row["thumbnail_revision"] or 1)
        if media_handle:
            revision += 1
        content_state = live_activities.build_content_state(
            level=card.level, mutation=mutation,
            glyph=live_activities.glyph_for(
                family or "", subject_kind=subject_kind, label=label, mutation=mutation,
            ),
            primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
            card_key=card_key, thumbnail_handle=media_handle, thumbnail_revision=revision,
        )
        payload = live_activities.build_la_update_payload(content_state=content_state, now=now)
        await transport.send_live_activity(
            device, token=row["token"], payload=payload, collapse_id=card_key, event="update",
        )
        store.touch_activity(
            conn, row["activity_id"], thumbnail_revision=revision, pushed=True, now=now,
        )
        store.record_activity_send(conn, activity_id=row["activity_id"], now=now)


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

    snapshot, subject_kind, place_class = snapshot_from_review(
        event, zone_place_map=config.delivery_zone_place_map,
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded,
    )
    level = evaluate_ladder(snapshot)
    zone_name = event.zones[0] if event.zones else ""
    track_ids = event.track_ids or (event.event_id,)

    mutated = 0
    for track_id in track_ids:
        card_key, existing, owning_camera = _resolve_card_for_track(
            conn, camera=event.camera, track_id=track_id, subject_kind=subject_kind,
            zone_name=zone_name, now=now,
        )
        card, mutation, sound = _advance_card(existing, level, card_key=card_key, now=now)

        elapsed = max(0.0, now - card.state_since_at)
        primary, secondary = _copy(
            subject_kind, snapshot.label, owning_camera, zone_name, elapsed,
        )
        if owning_camera != event.camera:
            # A second camera is now contributing to a card it didn't
            # create -- surface that in the copy rather than silently
            # merging (docs "Cross-camera deduplication" §2). The card's
            # own `camera` field stays the original, first-seen camera
            # (below) so the app's timeline routing is unaffected.
            secondary = f"{secondary} · also on {event.camera.replace('_', ' ').title()}"

        payload = None
        warm_task = None
        media_handle = None
        if should_push(card.level):
            media_handle, media, warm_task = _media_for(
                mutation, event, conn=conn, engine=engine, config=config,
            )
            payload = build_card_payload(
                card, mutation, sound=sound, subject_kind=subject_kind, place_class=place_class,
                camera=owning_camera, zone_name=zone_name,
                glyph=_glyph_for(subject_kind, snapshot.label),
                primary=primary, secondary=secondary, event_ts=now, media=media,
            )

        await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=subject_kind, place_class=place_class,
            camera=owning_camera, zone_name=zone_name,
        )
        if warm_task is not None:
            # Runs concurrently with the send above, not in series (plan §4
            # lever 4's rule, reused here): the push already carries the
            # `media` URL optimistically, so a slow or failed Frigate fetch
            # costs the notification its image, never its existence.
            with contextlib.suppress(Exception):
                await warm_task

        family = live_activities.should_start_activity(
            subject_kind=subject_kind, label=snapshot.label, place_class=place_class,
            families_enabled=config.delivery_la_families,
        )
        await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=family, camera=owning_camera, subject_kind=subject_kind,
            label=snapshot.label, primary=primary, secondary=secondary,
            elapsed_seconds=int(elapsed), media_handle=media_handle, now=now,
        )
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
    )
    return build_card_payload(
        card, "escalate", sound=True,
        subject_kind=subject_kind, place_class=context.get("place_class", ""),
        camera=context.get("camera", ""), zone_name=context.get("zone_name", ""),
        glyph=_glyph_for(subject_kind, ""), primary=primary, secondary=secondary, event_ts=now,
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
        elapsed = max(0.0, now - card.state_since_at)
        primary, secondary = _copy(kind, "", camera, zone_name, elapsed)
        payload = None
        if should_push(card.level):
            payload = build_card_payload(
                card, mutation, sound=sound, subject_kind=kind, place_class="",
                camera=camera, zone_name=zone_name, glyph=_glyph_for(kind, ""),
                primary=primary, secondary=secondary, event_ts=now,
            )
        await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=kind, camera=camera, zone_name=zone_name,
        )
        await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=None, camera=camera, subject_kind=kind, label="",
            primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
            media_handle=None, now=now,
        )
        resolved += 1
    return resolved
