"""Ties the decision engine, situation evaluation, handle store, and transport
together.

`PushEngine.handle_review_message` is the single entry point the MQTT
subscriber (and the offline-recovery backfill) calls per review message; it
is plain `async def` and takes its dependencies explicitly so it's testable
without a running app or a real MQTT connection.

**Two evaluation paths, one deploy** (notification-experience plan §8's
backward-compatibility rule, handoff item 2):

* A device with no `situations` stays on the v1 camera + label + severity
  subscription, firing exactly what it fires today. Phones running an older
  app build must not lose a single push on the sidecar upgrade.
* A device with a non-empty `situations` array switches to situation-only
  evaluation: its v1 filters survive as a cheap pre-filter, but they no
  longer trigger anything on their own.

The second path is the point of the whole phase -- the plan's problem
statement is that today's system is *correct* and *annoying*, dozens of
alerts a day where a house has two or three situations actually worth
interrupting anyone for.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from frigate_sidecar.push import activity as activity_payload
from frigate_sidecar.push import store
from frigate_sidecar.push.decision import (
    devices_for_event,
    matches,
    parse_object_message,
    parse_review_message,
)
from frigate_sidecar.push.library import sound_file
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.payload import build_payload
from frigate_sidecar.push.situations import (
    STAGE_ARRIVING,
    STAGE_ENDING,
    STAGE_ESCALATED,
    STAGE_PRESENT,
    Match,
    Situation,
    TrackStore,
    escalation_reached,
    evaluate_device,
    matches_conditions,
    present_situations,
)
from frigate_sidecar.push.thumbnails import fetch_thumbnail
from frigate_sidecar.push.transport import PushTransport, TransportResult

logger = logging.getLogger(__name__)


@dataclass
class PushEngine:
    db_path: str
    transport: PushTransport
    server_id: str
    handle_ttl_s: float = 3600.0
    #: Situation handles hold a pre-warmed thumbnail and outlive the v1 ones:
    #: plan §8 keeps them for 24h so a notification the user comes back to
    #: hours later can still redeem its image.
    situation_handle_ttl_s: float = 86400.0
    #: Where the pre-warm fetches from. Empty disables thumbnail warm-up
    #: entirely -- pushes still fire, image-less.
    frigate_base_url: str = ""
    #: Plan §6: max N pushes per situation per device per hour. Protects
    #: against a runaway camera (a flapping curtain producing 40 buzzes), which
    #: is a different problem from snooze -- that one protects against the
    #: user's own choice.
    rate_limit_per_hour: int = 10
    rate_limit_window_s: float = 3600.0
    thumbnail_max_edge: int = 320
    thumbnail_quality: int = 60
    thumbnail_timeout_s: float = 5.0
    gc_interval_s: float = 300.0

    #: Per-`(camera, track_id)` dwell state -- the only way to derive loiter
    #: from a topic that doesn't carry it. Wiped on every MQTT reconnect.
    tracks: TrackStore = field(default_factory=TrackStore)
    #: Where dwell comes from. "events" reads live occupancy off
    #: `frigate/events`; "reviews" is the handoff's literal prescription --
    #: dwell advanced only by `frigate/reviews` `type: update` messages --
    #: kept switchable, but see `push.situations` for why it does not fire on
    #: a real deployment.
    dwell_source: str = "events"

    # -- Phase 2: Live Activities --
    #: Coalescing floor for update pushes: no more than one per activity per
    #: this many seconds, however busy the object stream is.
    activity_update_min_interval_s: float = 3.0
    #: Quiet period after which a Present situation is considered resolved.
    activity_resolution_s: float = 30.0
    #: How long the activity lingers on screen after the end push.
    activity_dismissal_tail_s: float = 30.0
    #: The separate, higher LA budget -- iOS meters these independently of
    #: alerts, and a silent update is nothing like a buzz.
    activity_updates_per_hour: int = 60
    #: How long an ended activity's row survives before reaping, measured past
    #: its dismissal so a late token upload still finds something.
    activity_reap_after_s: float = 300.0

    _http: httpx.AsyncClient | None = None
    _warned_undeliverable: set[str] = field(default_factory=set)
    _last_gc: float = 0.0
    #: The most recent review message mentioning each `(camera, track_id)`.
    #: A review is what makes an object push-worthy; the object stream only
    #: says whether it is still there. Held so a loiter threshold crossed
    #: between review messages -- which is the normal case, since a person
    #: standing still generates no review traffic -- can still be noticed.
    _pending: dict[tuple[str, str], ReviewEvent] = field(default_factory=dict)
    #: When each `_pending` entry was set. A track this fresh has, by
    #: definition, not yet had its first `frigate/events` tick recorded in
    #: `tracks` -- that tick is exactly what `_pending` is waiting for. GC
    #: must not treat "not in `tracks` yet" as "stale"; see `_maybe_gc`.
    _pending_since: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Latest `sub_label` seen per `(camera, track_id)` off `frigate/events`.
    #: Feeds the `sub_label_unknown` escalation trigger; Phase 5 owns the
    #: allow/deny lists that will use the same input.
    _sub_labels: dict[tuple[str, str], str] = field(default_factory=dict)

    def _conn(self) -> sqlite3.Connection:
        from frigate_sidecar import db

        return db.open_sidecar(self.db_path)

    def _log_decision(
        self,
        *,
        apns_token: str,
        event_id: str,
        decision: str,
        uses_situations: bool,
        situation_id: str = "",
        reason: str = "",
    ) -> None:
        """One line per push decision (handoff Thread B item 4): fired,
        suppressed-by-snooze, suppressed-by-window, matched-no-situation,
        activity-started/updated/escalated/ended, and so on. Info level,
        structured, so a trace like Thread A's ("why didn't this fire")
        doesn't need a live repro to answer -- grep the token and the event.

        `uses_situations` is carried alongside the rest so the same grep that
        answers "what happened to this push" also answers "which mode was
        this device in when it happened" -- no separate lookup against the
        registration log required.
        """
        logger.info(
            "push: decision=%s apns_token=%s situation_id=%s event_id=%s "
            "uses_situations=%s reason=%s",
            decision, apns_token[:8], situation_id or "-", event_id or "-",
            uses_situations, reason or "-",
        )

    def _client(self) -> httpx.AsyncClient:
        """One pooled client for snapshot pre-warm, kept for the process's
        life so a match doesn't pay connection setup to Frigate on the
        interrupt path."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self.thumbnail_timeout_s)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def reset_tracks(self) -> None:
        """Wipe per-track state -- called on every MQTT reconnect, since a
        Frigate restart reissues track ids and stale entries would then
        describe a different object entirely (handoff item 8)."""
        if len(self.tracks):
            logger.info("push: clearing %d track(s) after mqtt reconnect", len(self.tracks))
        self.tracks.clear()
        self._pending.clear()
        self._pending_since.clear()
        self._sub_labels.clear()

    async def handle_object_payload(self, payload: dict[str, Any]) -> int:
        """Dwell input from one `frigate/events` message.

        This topic never triggers a push by itself: a match here is only
        possible for a track a `frigate/reviews` message already declared
        push-worthy, whose rule was waiting on loiter. What it provides is the
        two things the review topic cannot -- live zone occupancy, so leaving
        resets the dwell, and a tick every few hundred milliseconds, so a
        threshold crossed while nothing else is happening is actually noticed.
        """
        if self.dwell_source != "events":
            return 0
        obj = parse_object_message(payload)
        if obj is None:
            return 0
        key = (obj.camera, obj.track_id)

        if obj.msg_type == "end":
            # Frigate says the object is gone. That is resolution, and a
            # faster signal than waiting out the quiet timeout -- but the
            # activities have to be ended before the track state they are
            # keyed on is dropped.
            ended = await self._end_activities_for_track(obj.camera, obj.track_id)
            self.tracks.forget(obj.camera, obj.track_id)
            self._pending.pop(key, None)
            self._pending_since.pop(key, None)
            self._sub_labels.pop(key, None)
            return ended

        now = time.time()
        self.tracks.observe_object(obj.camera, obj.track_id, obj.current_zones, now=now)
        if obj.sub_label:
            self._sub_labels[key] = obj.sub_label
        self._touch_activities(obj.camera, obj.track_id, now=now)
        # Housekeeping hangs off this topic as well as the review one: review
        # messages are rare enough on a quiet house (4 in 20 minutes, measured)
        # that hanging GC off them alone would leave handles and send records
        # accumulating for hours. `_maybe_gc` throttles itself.
        self._maybe_gc(now)

        review = self._pending.get(key)
        if review is None:
            return 0
        conn = self._conn()
        try:
            devices = [d for d in store.list_devices(conn) if d.uses_situations]
        finally:
            conn.close()
        if not devices:
            return 0
        return await self._dispatch_situations(devices, review, now=now)

    async def handle_review_payload(self, payload: dict[str, Any]) -> int:
        """Parse + dispatch one `frigate/reviews` message. Returns the number
        of devices notified (0 if nothing matched or the message wasn't
        actionable)."""
        event = parse_review_message(payload)
        if event is None:
            return 0
        return await self.handle_event(event)

    async def handle_event(self, event: ReviewEvent) -> int:
        now = time.time()
        conn = self._conn()
        try:
            devices = store.list_devices(conn)
        finally:
            conn.close()

        v1 = [d for d in devices if not d.uses_situations]
        v2 = [d for d in devices if d.uses_situations]

        sent = 0
        if v1:
            sent += await self._dispatch_v1(v1, event)
        if v2:
            # Track state is a property of Frigate's stream, not of any one
            # device, so it is recorded once per message before anybody is
            # evaluated against it.
            if self.dwell_source == "events":
                # Occupancy comes from `frigate/events`; seeding it from the
                # review's cumulative `zones` here would re-add a zone the
                # object has already left and undo the exit signal that makes
                # loiter mean anything.
                for track_id in event.track_ids or (event.review_id,):
                    key = (event.camera, track_id)
                    self._pending[key] = event
                    self._pending_since[key] = now
            else:
                self.tracks.observe(event, now=now)
            if event.severity == "alert":
                # An early-fire activity started off a `detection` review has
                # now earned its place; record it so resolution gives it the
                # full tail rather than the short one (handoff item 8).
                self._mark_promoted(event)
            sent += await self._dispatch_situations(v2, event, now=now)

        self._maybe_gc(now)
        return sent

    # -- v1 path: unchanged behaviour for devices with no situations ---------

    async def _dispatch_v1(self, devices: list[Device], event: ReviewEvent) -> int:
        matched = devices_for_event(devices, event)
        if not matched:
            return 0

        conn = self._conn()
        try:
            handle = store.mint_handle(
                conn,
                camera=event.camera,
                event_id=event.event_id,
                review_id=event.review_id,
                ttl_s=self.handle_ttl_s,
            )
            conn.commit()
        finally:
            conn.close()

        to_prune: list[str] = []
        sent = 0
        for device in matched:
            result = await self.transport.send(
                device,
                handle=handle,
                server_id=self.server_id,
                severity=event.severity,
                collapse_id=event.review_id,
            )
            sent += self._account(device, result, to_prune)

        if to_prune:
            self._prune(to_prune)
        return sent

    # -- v2 path: situation evaluation --------------------------------------

    def _passes_prefilter(self, device: Device, event: ReviewEvent) -> bool:
        """Handoff (Phase 1) item 7: the v1 filters survive as a cheap
        pre-filter, so a device that only cares about the doorbell never pays
        situation evaluation for the garden camera.

        One Phase 2 exception. `detection_tier_early_fire` exists to start an
        activity on the `detection` review that arrives ~500ms before the
        `alert` promotion (plan §4 lever 5); a device sitting at the default
        `min_severity: "alert"` would have that review dropped here, and the
        opt-in could never do anything. Camera and label still apply.
        """
        if matches(device, event):
            return True
        if event.severity != "detection":
            return False
        if not any(s.detection_tier_early_fire for s in present_situations(device)):
            return False
        relaxed = replace(device, min_severity="detection")
        return matches(relaxed, event)

    async def _dispatch_situations(
        self, devices: list[Device], event: ReviewEvent, *, now: float
    ) -> int:
        # One handle (and one thumbnail fetch) per situation+track, shared by
        # every device that matched it: the phones are being told about the
        # same moment, and warming the same JPEG twice would only add latency
        # to the second one.
        groups: dict[tuple[str, str], list[tuple[Device, Match]]] = defaultdict(list)
        live_activity: list[Device] = []
        for device in devices:
            if not self._passes_prefilter(device, event):
                continue
            self._warn_undeliverable(device)
            if device.can_live_activity:
                live_activity.append(device)
            # Present-tier situations are the Live Activity path -- unless this
            # device can't run one, in which case they fall back to an alert
            # push exactly as an interrupt-tier situation would (Phase 2
            # handoff item 9: "the app works without Phase 2").
            tiers = ("interrupt",) if device.can_live_activity else ("interrupt", "present")
            for hit in evaluate_device(device, event, self.tracks, now=now, tiers=tiers):
                # Claim the dwell here, while nothing has awaited yet, rather
                # than after the send returns. Object messages arrive every few
                # hundred milliseconds and a send takes longer than that, so a
                # claim made on the far side of the await would let the same
                # dwell be matched -- and pushed -- twice. Released again if
                # the send fails (see `_send_to_devices`).
                self.tracks.mark_fired(
                    event.camera, hit.track_id, device.apns_token, hit.situation.id
                )
                groups[(hit.situation.id, hit.track_id)].append((device, hit))

        sent = 0
        for (situation_id, track_id), items in groups.items():
            sent += await self._fire_group(event, situation_id, track_id, items, now=now)
        for device in live_activity:
            sent += await self._drive_activities(device, event, now=now)
        return sent

    async def _fire_group(
        self,
        event: ReviewEvent,
        situation_id: str,
        track_id: str,
        items: list[tuple[Device, Match]],
        *,
        now: float,
    ) -> int:
        conn = self._conn()
        try:
            handle = store.mint_handle(
                conn,
                camera=event.camera,
                event_id=event.event_id,
                review_id=event.review_id,
                ttl_s=self.situation_handle_ttl_s,
                situation_id=situation_id,
                track_id=track_id,
            )
            conn.commit()
        finally:
            conn.close()

        # Plan §4 lever 4: the warm-up runs *in parallel* with the send, never
        # in series. Interrupt-tier matches hit the outbound socket
        # immediately; the thumbnail lands beside them while APNs is still
        # carrying the alert.
        warm = asyncio.create_task(
            self.prewarm_thumbnail(handle, camera=event.camera, event_id=event.event_id)
        )
        try:
            sent = await self._send_to_devices(event, handle, items, now=now)
        finally:
            with __import__("contextlib").suppress(Exception):
                await warm
        return sent

    async def _send_to_devices(
        self,
        event: ReviewEvent,
        handle: str,
        items: list[tuple[Device, Match]],
        *,
        now: float,
    ) -> int:
        to_prune: list[str] = []
        sent = 0
        for device, match in items:
            decision = self._gate(device, match, event, now=now)
            if decision is None:
                # Snoozed or over the ceiling. The claim stays spent: when the
                # snooze lifts two minutes from now, firing about a dwell that
                # ended long ago would be a stale buzz, not a rescued one.
                continue
            payload = build_payload(
                match, handle=handle, server_id=self.server_id, suppressed=decision
            )
            result = await self.transport.send_situation(
                device, payload=payload, collapse_id=match.collapse_id
            )
            if result.ok:
                self._record_sent(device, match, now=now)
                self._log_decision(
                    apns_token=device.apns_token, event_id=event.event_id,
                    uses_situations=device.uses_situations,
                    situation_id=match.situation.id, decision="fired",
                    reason=f"zone={match.zone or '-'} dwell={match.dwell_s:.1f}s",
                )
            else:
                if not result.unregistered:
                    # Only a send that actually happened counts against the
                    # ceiling and burns the track's one shot at this dwell --
                    # a transport failure must stay retryable on the next
                    # update.
                    self.tracks.unmark_fired(
                        event.camera, match.track_id, device.apns_token, match.situation.id
                    )
                self._log_decision(
                    apns_token=device.apns_token, event_id=event.event_id,
                    uses_situations=device.uses_situations,
                    situation_id=match.situation.id, decision="send-failed",
                    reason=str(result.error or "unknown"),
                )
            sent += self._account(device, result, to_prune)

        if to_prune:
            self._prune(to_prune)
        return sent

    def _gate(
        self, device: Device, match: Match, event: ReviewEvent, *, now: float
    ) -> int | None:
        """Snooze + rate limit. Returns the suppressed-count to fold into the
        body, or None if this push must not go out at all."""
        conn = self._conn()
        try:
            snoozed = store.active_snoozes(conn, device.apns_token, now=now)
            if (
                "global" in snoozed
                or f"situation:{match.situation.id}" in snoozed
                or f"camera:{event.camera}" in snoozed
            ):
                self._log_decision(
                    apns_token=device.apns_token, event_id=event.event_id,
                    uses_situations=device.uses_situations,
                    situation_id=match.situation.id, decision="suppressed-by-snooze",
                    reason=f"snoozed scopes: {sorted(snoozed)}",
                )
                return None

            recent = store.count_sends_since(
                conn,
                apns_token=device.apns_token,
                situation_id=match.situation.id,
                since=now - self.rate_limit_window_s,
            )
            if recent >= self.rate_limit_per_hour:
                store.bump_suppressed(
                    conn, apns_token=device.apns_token, situation_id=match.situation.id
                )
                conn.commit()
                self._log_decision(
                    apns_token=device.apns_token, event_id=event.event_id,
                    uses_situations=device.uses_situations,
                    situation_id=match.situation.id, decision="suppressed-by-window",
                    reason=f"{recent} sent in the last {self.rate_limit_window_s:.0f}s",
                )
                return None

            suppressed = store.take_suppressed(
                conn, apns_token=device.apns_token, situation_id=match.situation.id
            )
            conn.commit()
            return suppressed
        finally:
            conn.close()

    def _record_sent(self, device: Device, match: Match, *, now: float) -> None:
        """Charge one push against the rolling hourly window (plan §6).

        The once-per-dwell claim (handoff item 9) was already taken before the
        send -- see `_dispatch_situations`.
        """
        conn = self._conn()
        try:
            store.record_send(
                conn, apns_token=device.apns_token, situation_id=match.situation.id, now=now
            )
            conn.commit()
        finally:
            conn.close()

    # -- Phase 2: Live Activities -------------------------------------------

    async def _drive_activities(
        self, device: Device, event: ReviewEvent, *, now: float
    ) -> int:
        """Run the stage machine for one device's Present-tier situations.

        Called on every message that reaches situation evaluation -- reviews
        and `frigate/events` alike -- because an activity's whole job is to
        track something that is still happening. Emits at most one push per
        (situation, track) per call, and the coalescing window keeps that from
        becoming a stream.
        """
        sent = 0
        track_ids = event.track_ids or (event.review_id,)
        for situation in present_situations(device):
            for track_id in track_ids:
                sent += await self._drive_one(device, situation, event, track_id, now=now)
        return sent

    async def _drive_one(
        self, device: Device, situation: Situation, event: ReviewEvent,
        track_id: str, *, now: float,
    ) -> int:
        stage = self.tracks.stage(event.camera, track_id, device.apns_token, situation.id)
        hit = matches_conditions(situation, device, event, track_id, self.tracks, now=now)

        if hit is None:
            # The conditions stopped holding -- the object left the zone, or
            # the time window closed. Plan §3: "They leave -> LA ends with 30s
            # tail." Leaving is resolution, and a faster signal than waiting
            # out the quiet timeout.
            if stage is not None and stage != STAGE_ENDING:
                await self._end_activity(
                    device, situation, event.camera, track_id, reason="left", now=now
                )
            else:
                self._log_decision(
                    apns_token=device.apns_token, event_id=event.event_id,
                    uses_situations=device.uses_situations,
                    situation_id=situation.id, decision="matched-no-situation",
                    reason="camera/label/zone/time-of-day conditions not met",
                )
            return 0

        if stage is None:
            self._log_decision(
                apns_token=device.apns_token, event_id=event.event_id,
                uses_situations=device.uses_situations,
                situation_id=situation.id, decision="activity-started",
                reason=f"zone={hit.zone or '-'} dwell={hit.dwell_s:.1f}s",
            )
            return await self._start_activity(device, hit, event, now=now)

        if stage == STAGE_ENDING:
            return 0

        sub_label = self._sub_labels.get((event.camera, track_id), "")
        if stage != STAGE_ESCALATED and escalation_reached(hit, sub_label=sub_label):
            self._log_decision(
                apns_token=device.apns_token, event_id=event.event_id,
                uses_situations=device.uses_situations,
                situation_id=situation.id, decision="activity-escalated",
                reason=f"dwell={hit.dwell_s:.1f}s",
            )
            return await self._escalate(device, hit, event, now=now)

        return await self._update_activity(device, hit, event, stage=stage, now=now)

    async def _start_activity(
        self, device: Device, match: Match, event: ReviewEvent, *, now: float
    ) -> int:
        """Ask iOS to create the activity, via the device's push-to-start token."""
        conn = self._conn()
        try:
            handle = store.mint_handle(
                conn, camera=event.camera, event_id=event.event_id,
                review_id=event.review_id, ttl_s=self.situation_handle_ttl_s,
                situation_id=match.situation.id, track_id=match.track_id,
            )
            snoozed = store.active_snoozes(conn, device.apns_token, now=now)
            conn.commit()
        finally:
            conn.close()

        # A snoozed situation should not sprout a Live Activity either. The
        # user asked for quiet, and an LA is still a thing appearing on their
        # lock screen.
        if (
            "global" in snoozed
            or f"situation:{match.situation.id}" in snoozed
            or f"camera:{event.camera}" in snoozed
        ):
            return 0

        # Same parallel warm-up as the alert path: the widget fetches this
        # thumbnail by handle exactly as the NSE does.
        warm = asyncio.create_task(
            self.prewarm_thumbnail(handle, camera=event.camera, event_id=event.event_id)
        )
        # The sidecar has no activity id until the app uploads one; it needs a
        # key now, so it mints its own and the app's `activity_id` is reconciled
        # onto the same (device, situation, track) tuple when it arrives.
        activity_id = f"a_{secrets.token_urlsafe(8)}"
        from_detection = event.severity == "detection"
        try:
            payload = activity_payload.build_start(
                match, handle=handle, camera=event.camera,
                server_id=self.server_id, now=now,
            )
            result = await self.transport.send_live_activity(
                device, token=device.push_to_start_token, payload=payload,
                collapse_id=match.collapse_id, event="start",
            )
        finally:
            with __import__("contextlib").suppress(Exception):
                await warm

        if not result.ok:
            logger.warning(
                "push: LA start failed for device %s situation %s: %s",
                device.device_id, match.situation.id, result.error,
            )
            return 0

        conn = self._conn()
        try:
            store.open_activity(
                conn, activity_id=activity_id, apns_token=device.apns_token,
                situation_id=match.situation.id, track_id=match.track_id,
                camera=event.camera, collapse_id=match.collapse_id, handle=handle,
                from_detection=from_detection, now=now,
            )
            store.touch_activity(
                conn, activity_id, pushed=True, dwell_seconds=int(match.dwell_s), now=now
            )
            store.record_activity_send(conn, activity_id=activity_id, now=now)
            conn.commit()
        finally:
            conn.close()
        self.tracks.set_stage(
            event.camera, match.track_id, device.apns_token, match.situation.id,
            STAGE_ARRIVING,
        )
        logger.info(
            "push: LA start situation=%s track=%s device=%s activity=%s%s",
            match.situation.id, match.track_id, device.device_id, activity_id,
            " (early-fire off a detection review)" if from_detection else "",
        )
        return 1

    async def _update_activity(
        self, device: Device, match: Match, event: ReviewEvent, *, stage: str, now: float
    ) -> int:
        """A silent state change on the per-activity token."""
        conn = self._conn()
        try:
            row = store.find_activity(
                conn, apns_token=device.apns_token,
                situation_id=match.situation.id, track_id=match.track_id,
            )
        finally:
            conn.close()
        if row is None or not row["token"]:
            # iOS hasn't handed the app a per-activity token yet (or the app
            # hasn't uploaded it). The activity is on screen and will catch up
            # on the next observation; there is nothing to send it to now.
            return 0

        next_stage = STAGE_PRESENT if stage == STAGE_ARRIVING else stage
        # Coalesce: item 5 caps this at one push per 3s per activity. Without
        # it a busy track would emit five pushes a second against an iOS
        # budget that is not generous.
        if now - float(row["last_push_at"] or 0) < self.activity_update_min_interval_s:
            return 0
        if next_stage == stage and int(match.dwell_s) == int(row["dwell_seconds"]):
            # Nothing the device doesn't already show.
            return 0
        if not self._activity_budget_ok(row["activity_id"], now=now):
            return 0

        payload = activity_payload.build_update(
            match, stage=next_stage,
            thumbnail_revision=int(row["thumbnail_revision"]), now=now,
        )
        result = await self.transport.send_live_activity(
            device, token=row["token"], payload=payload,
            collapse_id=match.collapse_id, event="update",
        )
        if not result.ok:
            self._handle_activity_failure(row, result)
            return 0

        conn = self._conn()
        try:
            store.touch_activity(
                conn, row["activity_id"], stage=next_stage, pushed=True,
                dwell_seconds=int(match.dwell_s), now=now,
            )
            store.record_activity_send(conn, activity_id=row["activity_id"], now=now)
            conn.commit()
        finally:
            conn.close()
        self.tracks.set_stage(
            event.camera, match.track_id, device.apns_token, match.situation.id, next_stage
        )
        return 1

    async def _escalate(
        self, device: Device, match: Match, event: ReviewEvent, *, now: float
    ) -> int:
        """The Present situation crossed the interrupt bar.

        One `update`-shaped live-activity push carrying an `alert` sub-key and
        a sound at the `aps` level. iOS 17.2+ delivers that as a single event:
        the ContentState advances to `.escalated`, the banner shows, the sound
        plays.

        It is deliberately *not* a Phase 1 alert push any more. An alert with a
        matching `apns-collapse-id` collapses in Notification Center but cannot
        advance a Live Activity's ContentState, so the two surfaces drift --
        a banner announcing the escalation over an activity still rendering
        `.present` (plan amended, Elsinore `98e447e`).

        The alert shape survives as the fallback for a device that has no
        activity to escalate: the start push failed, or iOS hasn't handed the
        app a per-activity token yet. Buzzing without advancing an activity
        beats not buzzing at all.
        """
        conn = self._conn()
        try:
            row = store.find_activity(
                conn, apns_token=device.apns_token,
                situation_id=match.situation.id, track_id=match.track_id,
            )
        finally:
            conn.close()

        # Consume the transition *before* gating. Object messages arrive
        # several times a second, so an escalation left retryable would call
        # `_gate` on every one of them -- and each rate-limited call bumps the
        # suppressed counter, turning "+1 more" into "+2000 more" for a single
        # situation that buzzed once. Phase 1 avoids this by claiming the dwell
        # before the gate; this is the same move at the stage level.
        self.tracks.set_stage(
            event.camera, match.track_id, device.apns_token, match.situation.id,
            STAGE_ESCALATED,
        )
        self.tracks.mark_fired(
            event.camera, match.track_id, device.apns_token, match.situation.id
        )

        gate = self._gate(device, match, event, now=now)
        if gate is None:
            return 0

        revision = int(row["thumbnail_revision"]) + 1 if row else 1
        handle = row["handle"] if row else ""
        if not handle:
            conn = self._conn()
            try:
                handle = store.mint_handle(
                    conn, camera=event.camera, event_id=event.event_id,
                    review_id=event.review_id, ttl_s=self.situation_handle_ttl_s,
                    situation_id=match.situation.id, track_id=match.track_id,
                )
                conn.commit()
            finally:
                conn.close()

        # Refresh the image behind the *existing* handle rather than minting a
        # new one. `handle` is on the activity's static attributes and cannot
        # change mid-activity, so a fresh snapshot has to arrive under the same
        # key -- which is exactly what `thumbnail_revision` is for: the widget
        # refetches the same URL and gets the newer bytes.
        await self.prewarm_thumbnail(handle, camera=event.camera, event_id=event.event_id)

        if row is not None and row["token"]:
            payload = activity_payload.build_escalation(
                match, sound=sound_file(match.situation.sound),
                thumbnail_revision=revision, now=now,
            )
            result = await self.transport.send_live_activity(
                device, token=row["token"], payload=payload,
                collapse_id=match.collapse_id, event="update",
            )
        else:
            # No activity to advance -- the start failed, or iOS hasn't handed
            # the app a per-activity token yet. Fall back to the alert shape so
            # the user is still told.
            logger.info(
                "push: escalating %s for device %s without a live activity "
                "(%s) -- falling back to an alert push",
                match.situation.id, device.device_id,
                "no activity row" if row is None else "token not uploaded yet",
            )
            payload = build_payload(
                match, handle=handle, server_id=self.server_id, suppressed=gate, now=now
            )
            result = await self.transport.send_situation(
                device, payload=payload, collapse_id=match.collapse_id
            )
        if not result.ok:
            to_prune: list[str] = []
            self._account(device, result, to_prune)
            if to_prune:
                self._prune(to_prune)
            elif not result.unregistered:
                # A transport blip is the one failure worth retrying: hand the
                # transition back so the next observation tries again. Snooze
                # and the rate limit, above, stay consumed.
                self.tracks.set_stage(
                    event.camera, match.track_id, device.apns_token,
                    match.situation.id, STAGE_PRESENT,
                )
                self.tracks.unmark_fired(
                    event.camera, match.track_id, device.apns_token, match.situation.id
                )
            return 0

        # Charged against the alert ceiling either way: this is the thing that
        # buzzes, and that budget is what protects the user from interrupt
        # spam regardless of which wire shape carried it.
        self._record_sent(device, match, now=now)
        if row is not None:
            conn = self._conn()
            try:
                store.touch_activity(
                    conn, row["activity_id"], stage=STAGE_ESCALATED, pushed=True,
                    thumbnail_revision=revision, dwell_seconds=int(match.dwell_s), now=now,
                )
                if row["token"]:
                    store.record_activity_send(conn, activity_id=row["activity_id"], now=now)
                conn.commit()
            finally:
                conn.close()
        logger.info(
            "push: LA escalation situation=%s track=%s device=%s collapse_id=%s",
            match.situation.id, match.track_id, device.device_id, match.collapse_id,
        )
        return 1

    async def _end_activity(
        self,
        device: Device,
        situation: Situation,
        camera: str,
        track_id: str,
        *,
        reason: str,
        now: float,
        row: Any = None,
    ) -> int:
        """Resolution: end the activity with a tail so it fades rather than
        vanishing."""
        if row is None:
            conn = self._conn()
            try:
                row = store.find_activity(
                    conn, apns_token=device.apns_token,
                    situation_id=situation.id, track_id=track_id,
                )
            finally:
                conn.close()
        self.tracks.set_stage(camera, track_id, device.apns_token, situation.id, STAGE_ENDING)
        if row is None:
            return 0

        # An early-fire activity that never earned its alert promotion gets a
        # shorter tail: it was a guess, and it should leave quickly once the
        # guess doesn't pan out (handoff item 8).
        unpromoted = bool(row["from_detection"]) and not bool(row["promoted"])
        tail = (
            activity_payload.UNPROMOTED_DISMISSAL_TAIL_S if unpromoted
            else self.activity_dismissal_tail_s
        )

        sent = 0
        if row["token"]:
            match = Match(
                situation=situation, track_id=track_id,
                dwell_s=float(row["dwell_seconds"] or 0), label="", zone="",
            )
            payload = activity_payload.build_end(
                match, thumbnail_revision=int(row["thumbnail_revision"]),
                tail_s=tail, now=now,
            )
            result = await self.transport.send_live_activity(
                device, token=row["token"], payload=payload,
                collapse_id=row["collapse_id"], event="end",
            )
            if result.ok:
                sent = 1
            else:
                logger.warning(
                    "push: LA end failed for activity %s: %s",
                    row["activity_id"], result.error,
                )

        conn = self._conn()
        try:
            store.close_activity(conn, row["activity_id"], now=now)
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "push: LA end situation=%s track=%s activity=%s reason=%s tail=%.0fs%s",
            situation.id, track_id, row["activity_id"], reason, tail,
            " (unpromoted early-fire)" if unpromoted else "",
        )
        return sent

    def _mark_promoted(self, event: ReviewEvent) -> None:
        conn = self._conn()
        try:
            changed = False
            for track_id in event.track_ids or (event.review_id,):
                cur = conn.execute(
                    "UPDATE push_activities SET promoted = 1 WHERE camera = ? AND track_id = ? "
                    "AND ended_at IS NULL AND from_detection = 1 AND promoted = 0",
                    (event.camera, track_id),
                )
                changed = changed or cur.rowcount > 0
            if changed:
                conn.commit()
        finally:
            conn.close()

    def _touch_activities(self, camera: str, track_id: str, *, now: float) -> None:
        """Mark this track's activities as still-being-observed.

        What keeps the resolution sweeper from ending an activity while the
        object is plainly still there -- and, equally, what lets it end one the
        moment the observations stop.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT activity_id FROM push_activities WHERE camera = ? AND track_id = ? "
                "AND ended_at IS NULL", (camera, track_id),
            ).fetchall()
            for row in rows:
                store.touch_activity(conn, row["activity_id"], seen=True, now=now)
            if rows:
                conn.commit()
        finally:
            conn.close()

    async def _end_activities_for_track(self, camera: str, track_id: str) -> int:
        """End every open activity for a track Frigate just closed."""
        now = time.time()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM push_activities WHERE camera = ? AND track_id = ? "
                "AND ended_at IS NULL", (camera, track_id),
            ).fetchall()
            devices = {d.apns_token: d for d in store.list_devices(conn)} if rows else {}
        finally:
            conn.close()

        sent = 0
        for row in rows:
            device = devices.get(row["apns_token"])
            if device is None:
                continue
            situation = next(
                (s for s in device.situations if s.id == row["situation_id"]),
                Situation(id=row["situation_id"], name=row["situation_id"]),
            )
            sent += await self._end_activity(
                device, situation, camera, track_id, reason="object-ended", now=now, row=row
            )
        return sent

    async def sweep_activities(self, *, now: float | None = None) -> int:
        """End activities whose situation has gone quiet (handoff item 7).

        Resolution is the one transition no incoming message announces --
        nothing arrives to say "the object stopped being reported" -- so it
        needs a sweep. Deliberately *not* a keep-alive: this only ever ends
        activities, never refreshes them, so the non-negotiable that stage
        transitions are the only thing minting an LA push still holds.
        """
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            stale = store.stale_activities(
                conn, quiet_for=self.activity_resolution_s, now=now
            )
            devices = {d.apns_token: d for d in store.list_devices(conn)}
        finally:
            conn.close()

        sent = 0
        for row in stale:
            device = devices.get(row["apns_token"])
            if device is None:
                conn = self._conn()
                try:
                    store.delete_activity(conn, row["activity_id"])
                    conn.commit()
                finally:
                    conn.close()
                continue
            situation = next(
                (s for s in device.situations if s.id == row["situation_id"]), None
            )
            if situation is None:
                situation = Situation(id=row["situation_id"], name=row["situation_id"])
            sent += await self._end_activity(
                device, situation, row["camera"], row["track_id"],
                reason="quiet", now=now, row=row,
            )
        return sent

    def _activity_budget_ok(self, activity_id: str, *, now: float) -> bool:
        """The separate, higher LA budget (handoff: 60/hour/activity).

        Kept apart from the alert tier's 10/hour in both directions: a chatty
        activity must not eat the budget that a genuine interrupt needs, and a
        silent update is nothing like a buzz.
        """
        conn = self._conn()
        try:
            count = store.count_activity_sends(
                conn, activity_id=activity_id, since=now - self.rate_limit_window_s
            )
        finally:
            conn.close()
        if count >= self.activity_updates_per_hour:
            logger.info(
                "push: LA update budget reached for activity %s (%d in the window)",
                activity_id, count,
            )
            return False
        return True

    def _handle_activity_failure(self, row: Any, result: TransportResult) -> None:
        """A dead *activity* token is not a dead device.

        410/`BadDeviceToken` on an update means iOS has torn this activity
        down -- the user swiped it away, or it aged out. Pruning the device row
        for that (the alert path's response to the same status) would
        unregister a perfectly good phone.
        """
        if result.unregistered:
            logger.info(
                "push: activity %s token is dead (%s) -- closing the activity, "
                "device row untouched", row["activity_id"], result.error,
            )
            conn = self._conn()
            try:
                store.close_activity(conn, row["activity_id"])
                conn.commit()
            finally:
                conn.close()
        else:
            logger.warning(
                "push: LA update failed for activity %s: %s",
                row["activity_id"], result.error,
            )

    async def prewarm_thumbnail(self, handle: str, *, camera: str, event_id: str) -> bool:
        """Fetch, shrink, and park the snapshot under `handle` (plan §4 lever 1).

        Returns whether an image landed. False is a normal outcome, not an
        error: Frigate may be down, the event may be too fresh to have a
        snapshot, the fetch may time out. The push has already gone out.
        """
        if not self.frigate_base_url:
            return False
        jpeg = await fetch_thumbnail(
            self._client(),
            frigate_base_url=self.frigate_base_url,
            camera=camera,
            event_id=event_id,
            max_edge=self.thumbnail_max_edge,
            quality=self.thumbnail_quality,
            timeout=self.thumbnail_timeout_s,
        )
        if not jpeg:
            logger.info(
                "push: no thumbnail for handle %s (camera=%s event=%s) -- the push still "
                "fires, it just lands without an image",
                handle, camera, event_id,
            )
            return False
        conn = self._conn()
        try:
            store.store_thumbnail(conn, handle, jpeg)
            conn.commit()
        finally:
            conn.close()
        return True

    # -- shared bookkeeping --------------------------------------------------

    def _account(self, device: Device, result: TransportResult, to_prune: list[str]) -> int:
        if result.ok:
            return 1
        if result.unregistered:
            # 410 Unregistered / 400 BadDeviceToken (spec §5) -- permanent,
            # prune immediately rather than waiting for a retry to fail
            # again.
            to_prune.append(device.apns_token)
            logger.info("push: pruning device %s (%s)", device.device_id, result.error)
        else:
            logger.warning(
                "push: send failed for device %s: %s", device.device_id, result.error
            )
        return 0

    def _warn_undeliverable(self, device: Device) -> None:
        """Say once, per device, that some of its situations can't be
        delivered yet -- Present and Ambient tiers have no surface until Live
        Activities (Phase 2) and widgets (Phase 3). Silence here would look
        exactly like a dropped notification."""
        from frigate_sidecar.push.situations import undeliverable_tiers

        if device.device_id in self._warned_undeliverable:
            return
        self._warned_undeliverable.add(device.device_id)
        ids = undeliverable_tiers(device)
        if ids:
            logger.info(
                "push: device %s has situation(s) %s at a tier with no delivery surface "
                "yet (present/ambient land in Phase 2/3); they are evaluated and not sent",
                device.device_id, ", ".join(ids),
            )

    def _prune(self, tokens: list[str]) -> None:
        """Drop permanently-dead device rows (410/400, spec §5)."""
        conn = self._conn()
        try:
            for token in tokens:
                store.delete_device(conn, token)
            conn.commit()
        finally:
            conn.close()

    def _maybe_gc(self, now: float) -> None:
        """Periodic housekeeping, on the same thread that just did the work.

        Everything here is a cheap indexed DELETE and one dict sweep; running
        it inline every few minutes avoids a background task whose only job
        would be to wake up and find nothing to do.
        """
        if now - self._last_gc < self.gc_interval_s:
            return
        self._last_gc = now
        reaped = self.tracks.reap(now=now)
        # A pending review outlives its object only if Frigate never sent the
        # `end` -- drop those with the track they were waiting on. But a
        # track that hasn't reached `tracks` *yet* is not the same thing: a
        # review can (and often does) arrive before that track's first
        # `frigate/events` tick has been processed, and pruning on bare
        # absence here raced that first tick out from under it -- the pending
        # review, and the situation match it was holding open, vanished
        # before `frigate/events` ever got a chance to look it up (2026-08-05
        # doorbell miss). Age it out instead, on the same clock `tracks.reap`
        # already uses for "this track is actually gone".
        for key in [
            k for k in self._pending
            if k not in self.tracks
            and now - self._pending_since.get(k, now) > self.tracks.reap_after_s
        ]:
            del self._pending[key]
            self._pending_since.pop(key, None)
        for key in [k for k in self._sub_labels if k not in self.tracks]:
            del self._sub_labels[key]
        conn = self._conn()
        try:
            handles = store.prune_expired_handles(conn, now=now)
            snoozes = store.prune_expired_snoozes(conn, now=now)
            # Kept a window past the rate limiter's own so a restart mid-window
            # can still see the sends that came before it.
            sends = store.prune_old_sends(conn, older_than=self.rate_limit_window_s * 2, now=now)
            # Ended activities outlive their dismissal window by a margin, so a
            # token upload that arrives just after the end still lands on a row
            # rather than resurrecting one.
            activities = store.reap_activities(
                conn, older_than=self.activity_reap_after_s, now=now
            )
            conn.commit()
        finally:
            conn.close()
        if handles or reaped or snoozes or sends or activities:
            logger.debug(
                "push: gc dropped %d handle(s), %d track(s), %d snooze(s), %d send record(s), "
                "%d activity row(s)", handles, reaped, snoozes, sends, activities,
            )

    # -- test pushes ---------------------------------------------------------

    async def send_test(self, device: Device) -> TransportResult:
        """One test push to `device`, bypassing its subscription filters.

        Filters are deliberately not consulted: this verifies the APNs pipe, not
        the subscription (spec §1), so a device subscribed to one camera still
        gets its own test. Environment routing is *not* bypassed.

        The 410/400 feedback cleanup is the same one a real send applies -- a
        dead token discovered by pressing the test button is exactly as dead as
        one discovered by a real alert, and leaving the row behind would mean
        the next real alert rediscovers it.
        """
        result = await self.transport.send_test(device)
        if result.unregistered:
            logger.info(
                "push: pruning device %s after test send (%s)",
                device.device_id, result.error,
            )
            self._prune([device.apns_token])
        elif not result.ok:
            logger.warning(
                "push: test send failed for device %s: %s", device.device_id, result.error
            )
        return result

    async def send_situation_test(
        self, device: Device, situation: Situation, *, camera: str = ""
    ) -> TransportResult:
        """Fire one situation-shaped push at `device` through the real pipeline.

        Deliberately the *whole* path -- handle minting, thumbnail pre-warm,
        payload shape, collapse id, sound -- because what the app's Settings
        button is verifying is that a real situation push would arrive looking
        the way it should, not merely that APNs is reachable (spec §1's plain
        test push already answers that).

        Snooze and the rate limit are bypassed, and the send is not recorded
        against the hourly ceiling: pressing "test" is the user asking for
        this one, and it must not spend the budget a real alert might need
        ten minutes later.
        """
        camera = camera or (situation.cameras[0] if situation.cameras else "")
        camera = camera or (device.cameras[0] if device.cameras else "")
        track_id = f"test-{int(time.time())}"

        conn = self._conn()
        try:
            handle = store.mint_handle(
                conn,
                camera=camera,
                event_id="",
                review_id=f"test-{situation.id}",
                ttl_s=self.situation_handle_ttl_s,
                situation_id=situation.id,
                track_id=track_id,
            )
            conn.commit()
        finally:
            conn.close()

        match = Match(
            situation=situation,
            track_id=track_id,
            dwell_s=situation.loiter_seconds,
            label=situation.labels[0] if situation.labels else "person",
            zone=situation.zones[0] if situation.zones else "",
        )
        warm = asyncio.create_task(
            # No event id: a test has no tracked object, so the pre-warm falls
            # through to the camera's latest frame -- which is also the most
            # honest preview of what this situation would look like.
            self.prewarm_thumbnail(handle, camera=camera, event_id="")
        )
        try:
            payload = build_payload(match, handle=handle, server_id=self.server_id)
            result = await self.transport.send_situation(
                device, payload=payload, collapse_id=match.collapse_id
            )
        finally:
            with __import__("contextlib").suppress(Exception):
                await warm

        if result.unregistered:
            logger.info(
                "push: pruning device %s after situation test send (%s)",
                device.device_id, result.error,
            )
            self._prune([device.apns_token])
        elif not result.ok:
            logger.warning(
                "push: situation test send failed for device %s: %s",
                device.device_id, result.error,
            )
        return result
