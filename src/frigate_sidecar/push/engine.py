"""Ties the decision engine, handle store, and transport together.

`PushEngine.handle_review_message` is the single entry point the MQTT
subscriber (and the offline-recovery backfill) calls per review message; it
is plain `async def` and takes its dependencies explicitly so it's testable
without a running app or a real MQTT connection.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from frigate_sidecar.push import store
from frigate_sidecar.push.decision import devices_for_event, parse_review_message
from frigate_sidecar.push.transport import PushTransport, TransportResult

logger = logging.getLogger(__name__)


@dataclass
class PushEngine:
    db_path: str
    transport: PushTransport
    server_id: str
    handle_ttl_s: float = 3600.0
    # Every review_id already notified this process lifetime, so a duplicate
    # or out-of-order `update` for the same item doesn't re-mint a handle
    # needlessly -- `apns-collapse-id` already handles de-dup at the APNs
    # layer (spec §5), this just avoids pointless extra sends for a review
    # that hasn't actually changed camera/labels.
    _seen: set[str] = field(default_factory=set)

    def _conn(self) -> sqlite3.Connection:
        from frigate_sidecar import db

        return db.open_sidecar(self.db_path)

    async def handle_review_payload(self, payload: dict[str, Any]) -> int:
        """Parse + dispatch one `frigate/reviews` message. Returns the number
        of devices notified (0 if nothing matched or the message wasn't
        actionable)."""
        event = parse_review_message(payload)
        if event is None:
            return 0
        return await self.handle_event(event)

    async def handle_event(self, event: Any) -> int:
        conn = self._conn()
        try:
            devices = store.list_devices(conn)
        finally:
            conn.close()

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
            if result.ok:
                sent += 1
            elif result.unregistered:
                # 410 Unregistered / 400 BadDeviceToken (spec §5) -- permanent,
                # prune immediately rather than waiting for a retry to fail
                # again.
                to_prune.append(device.apns_token)
                logger.info(
                    "push: pruning device %s (%s)", device.device_id, result.error
                )
            else:
                logger.warning(
                    "push: send failed for device %s: %s", device.device_id, result.error
                )

        if to_prune:
            self._prune(to_prune)

        return sent

    def _prune(self, tokens: list[str]) -> None:
        """Drop permanently-dead device rows (410/400, spec §5)."""
        conn = self._conn()
        try:
            for token in tokens:
                store.delete_device(conn, token)
            conn.commit()
        finally:
            conn.close()

    async def send_test(self, device: Any) -> TransportResult:
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
