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
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx

from frigate_sidecar.push import store
from frigate_sidecar.push.decision import (
    devices_for_event,
    matches,
    parse_object_message,
    parse_review_message,
)
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.payload import build_payload
from frigate_sidecar.push.situations import Match, Situation, TrackStore, evaluate_device
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
    _http: httpx.AsyncClient | None = None
    _warned_undeliverable: set[str] = field(default_factory=set)
    _last_gc: float = 0.0
    #: The most recent review message mentioning each `(camera, track_id)`.
    #: A review is what makes an object push-worthy; the object stream only
    #: says whether it is still there. Held so a loiter threshold crossed
    #: between review messages -- which is the normal case, since a person
    #: standing still generates no review traffic -- can still be noticed.
    _pending: dict[tuple[str, str], ReviewEvent] = field(default_factory=dict)

    def _conn(self) -> sqlite3.Connection:
        from frigate_sidecar import db

        return db.open_sidecar(self.db_path)

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
            self.tracks.forget(obj.camera, obj.track_id)
            self._pending.pop(key, None)
            return 0

        now = time.time()
        self.tracks.observe_object(obj.camera, obj.track_id, obj.current_zones, now=now)
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
                    self._pending[(event.camera, track_id)] = event
            else:
                self.tracks.observe(event, now=now)
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

    async def _dispatch_situations(
        self, devices: list[Device], event: ReviewEvent, *, now: float
    ) -> int:
        # One handle (and one thumbnail fetch) per situation+track, shared by
        # every device that matched it: the phones are being told about the
        # same moment, and warming the same JPEG twice would only add latency
        # to the second one.
        groups: dict[tuple[str, str], list[tuple[Device, Match]]] = defaultdict(list)
        for device in devices:
            # Handoff item 7: the v1 filters survive as a cheap pre-filter, so
            # a device that only cares about the doorbell never pays situation
            # evaluation for the garden camera. They no longer fire anything
            # by themselves.
            if not matches(device, event):
                continue
            self._warn_undeliverable(device)
            for hit in evaluate_device(device, event, self.tracks, now=now):
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

        if not groups:
            return 0

        sent = 0
        for (situation_id, track_id), items in groups.items():
            sent += await self._fire_group(event, situation_id, track_id, items, now=now)
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
            elif not result.unregistered:
                # Only a send that actually happened counts against the
                # ceiling and burns the track's one shot at this dwell -- a
                # transport failure must stay retryable on the next update.
                self.tracks.unmark_fired(
                    event.camera, match.track_id, device.apns_token, match.situation.id
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
                logger.debug(
                    "push: %s snoozed for device %s", match.situation.id, device.device_id
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
                logger.info(
                    "push: rate limit reached for %s on device %s (%d in the last %.0fs) -- "
                    "suppressing until the window opens",
                    match.situation.id, device.device_id, recent, self.rate_limit_window_s,
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
        # `end` -- drop those with the track they were waiting on.
        for key in [k for k in self._pending if k not in self.tracks]:
            del self._pending[key]
        conn = self._conn()
        try:
            handles = store.prune_expired_handles(conn, now=now)
            snoozes = store.prune_expired_snoozes(conn, now=now)
            # Kept a window past the rate limiter's own so a restart mid-window
            # can still see the sends that came before it.
            sends = store.prune_old_sends(conn, older_than=self.rate_limit_window_s * 2, now=now)
            conn.commit()
        finally:
            conn.close()
        if handles or reaped or snoozes or sends:
            logger.debug(
                "push: gc dropped %d handle(s), %d track(s), %d snooze(s), %d send record(s)",
                handles, reaped, snoozes, sends,
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
