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
from frigate_sidecar.push.transport import PushTransport

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
            conn = self._conn()
            try:
                for token in to_prune:
                    store.delete_device(conn, token)
                conn.commit()
            finally:
                conn.close()

        return sent
