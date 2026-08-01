"""SQLite-backed storage for push devices and handles.

Uses the sidecar's own DB (`db.open_sidecar`) -- same pattern as the
scrub-cache tables, just a different pair of tables (`push_devices`,
`push_handles`, both in `db.SIDECAR_SCHEMA`).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from frigate_sidecar.push.models import Device


def device_id_for_token(apns_token: str) -> str:
    """Deterministic local handle derived from the token.

    Not a second identity -- re-registering the same token must yield the
    same `device_id` so a client that logs it for support purposes sees a
    stable value across the idempotent-PUT re-registrations the spec
    requires (§1: iOS reissues tokens rarely, but every relaunch re-PUTs the
    *current* one).
    """
    digest = hashlib.sha256(apns_token.encode()).hexdigest()
    return f"d_{digest[:10]}"


def _row_to_device(row: sqlite3.Row) -> Device:
    return Device(
        apns_token=row["apns_token"],
        device_id=row["device_id"],
        bundle_id=row["bundle_id"],
        environment=row["environment"],
        app_version=row["app_version"],
        cameras=tuple(json.loads(row["cameras"])),
        labels=tuple(json.loads(row["labels"])),
        min_severity=row["min_severity"],
    )


def upsert_device(
    conn: sqlite3.Connection,
    *,
    apns_token: str,
    bundle_id: str,
    environment: str,
    app_version: str = "",
    cameras: list[str] | None = None,
    labels: list[str] | None = None,
    min_severity: str = "alert",
) -> str:
    """Idempotent PUT on the token (spec §1) -- overwrites filter state in
    place rather than accumulating duplicate rows that would double-fire
    alerts. Returns the device_id."""
    device_id = device_id_for_token(apns_token)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO push_devices "
        "(apns_token, device_id, bundle_id, environment, app_version, cameras, labels, "
        " min_severity, registered_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(apns_token) DO UPDATE SET "
        "bundle_id=excluded.bundle_id, environment=excluded.environment, "
        "app_version=excluded.app_version, cameras=excluded.cameras, labels=excluded.labels, "
        "min_severity=excluded.min_severity, updated_at=excluded.updated_at",
        (
            apns_token, device_id, bundle_id, environment, app_version,
            json.dumps(cameras or []), json.dumps(labels or []), min_severity, now, now,
        ),
    )
    return device_id


def delete_device(conn: sqlite3.Connection, apns_token: str) -> bool:
    """Delete a device row. Used for explicit unregistration (DELETE
    /v1/push/devices/{token}) and 410/400 feedback-driven pruning (spec §5).
    Returns True if a row was actually removed."""
    cur = conn.execute("DELETE FROM push_devices WHERE apns_token = ?", (apns_token,))
    return cur.rowcount > 0


def get_device(conn: sqlite3.Connection, apns_token: str) -> Device | None:
    row = conn.execute(
        "SELECT * FROM push_devices WHERE apns_token = ?", (apns_token,)
    ).fetchone()
    return _row_to_device(row) if row else None


def list_devices(conn: sqlite3.Connection) -> list[Device]:
    rows = conn.execute("SELECT * FROM push_devices").fetchall()
    return [_row_to_device(r) for r in rows]


def mint_handle(
    conn: sqlite3.Connection,
    *,
    camera: str,
    event_id: str,
    review_id: str,
    ttl_s: float,
    now: float | None = None,
) -> str:
    """Mint an opaque, short-lived handle mapping to {camera, event_id}.

    Not the raw Frigate event id itself -- a Frigate id is
    `<start_time>-<rand>` and embeds a wall-clock timestamp, exactly the kind
    of incidental leak the spec's privacy line (§4) exists to avoid. The NSE
    and app redeem the handle server-side; they never see the raw id from
    the APNs payload.
    """
    now = time.time() if now is None else now
    handle = f"h_{secrets.token_urlsafe(8)}"
    conn.execute(
        "INSERT INTO push_handles (handle, camera, event_id, review_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (handle, camera, event_id, review_id, now, now + ttl_s),
    )
    return handle


def redeem_handle(
    conn: sqlite3.Connection, handle: str, *, now: float | None = None
) -> dict[str, str] | None:
    """Look up a handle, or None if it doesn't exist or has expired.

    Handles are single-use in the sense that they're short-lived, not that
    redemption consumes them -- the NSE (steps 2-3) and, potentially, the app
    itself both redeem the same handle from one push, and neither should
    race the other out of a result.
    """
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT camera, event_id FROM push_handles WHERE handle = ? AND expires_at > ?",
        (handle, now),
    ).fetchone()
    if row is None:
        return None
    return {"camera": row["camera"], "event_id": row["event_id"]}


def prune_expired_handles(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_handles WHERE expires_at <= ?", (now,))
    return cur.rowcount
