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
from typing import Any

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


def _col(row: sqlite3.Row, name: str, default: object = None) -> Any:
    """`row[name]`, or `default` when the column isn't present."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _json_or(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row_to_device(row: sqlite3.Row) -> Device:
    from frigate_sidecar.push.situations import parse_situations

    situations = parse_situations(_json_or(_col(row, "situations"), []))
    loc = _json_or(_col(row, "location"), None)
    location: tuple[float, float] | None = None
    if isinstance(loc, dict):
        try:
            location = (float(loc["lat"]), float(loc["lon"]))
        except (KeyError, TypeError, ValueError):
            location = None

    return Device(
        apns_token=row["apns_token"],
        device_id=row["device_id"],
        bundle_id=row["bundle_id"],
        environment=row["environment"],
        app_version=row["app_version"],
        cameras=tuple(json.loads(row["cameras"])),
        labels=tuple(json.loads(row["labels"])),
        min_severity=row["min_severity"],
        # The stored `schema_version` is what the client claimed; what decides
        # the evaluation path is `situations`, and a row with rules on it is a
        # v2 row whatever the client called itself.
        schema_version=2 if situations else int(_col(row, "schema_version", 1) or 1),
        timezone=str(_col(row, "timezone", "") or ""),
        location=location,
        situations=situations,
        live_activity_token=str(_col(row, "live_activity_token", "") or ""),
    )


# Registration fields this phase persists without reading. Named here so the
# later-phase handoffs have one place to look for what is already arriving.
IGNORED_REGISTRATION_FIELDS = ("live_activity_token", "morning_digest", "llm")


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
    schema_version: int = 1,
    timezone_name: str = "",
    location: dict[str, float] | None = None,
    situations: list[dict[str, Any]] | None = None,
    live_activity_token: str = "",
    morning_digest: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
) -> str:
    """Idempotent PUT on the token (spec §1) -- overwrites filter state in
    place rather than accumulating duplicate rows that would double-fire
    alerts. Returns the device_id.

    Every v2 field is persisted whether or not this phase evaluates it (plan
    §8 / handoff item 1), so an app build can start sending the full shape
    before the sidecar acts on all of it.
    """
    device_id = device_id_for_token(apns_token)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO push_devices "
        "(apns_token, device_id, bundle_id, environment, app_version, cameras, labels, "
        " min_severity, registered_at, updated_at, schema_version, timezone, location, "
        " situations, live_activity_token, morning_digest, llm) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(apns_token) DO UPDATE SET "
        "bundle_id=excluded.bundle_id, environment=excluded.environment, "
        "app_version=excluded.app_version, cameras=excluded.cameras, labels=excluded.labels, "
        "min_severity=excluded.min_severity, updated_at=excluded.updated_at, "
        "schema_version=excluded.schema_version, timezone=excluded.timezone, "
        "location=excluded.location, situations=excluded.situations, "
        "live_activity_token=excluded.live_activity_token, "
        "morning_digest=excluded.morning_digest, llm=excluded.llm",
        (
            apns_token, device_id, bundle_id, environment, app_version,
            json.dumps(cameras or []), json.dumps(labels or []), min_severity, now, now,
            int(schema_version), timezone_name,
            json.dumps(location) if location else None,
            json.dumps(situations or []), live_activity_token,
            json.dumps(morning_digest) if morning_digest is not None else None,
            json.dumps(llm) if llm is not None else None,
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
    situation_id: str = "",
    track_id: str = "",
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
        "INSERT INTO push_handles (handle, camera, event_id, review_id, created_at, expires_at, "
        "situation_id, track_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, camera, event_id, review_id, now, now + ttl_s, situation_id, track_id),
    )
    return handle


def store_thumbnail(conn: sqlite3.Connection, handle: str, jpeg: bytes) -> bool:
    """Attach the pre-warmed JPEG to an already-minted handle (plan §4 lever 1).

    Separate from `mint_handle` because the two happen in parallel, not in
    series: the push goes out the moment the situation matches, and the
    thumbnail lands beside it while APNs is still carrying the alert. A
    thumbnail that never arrives is a notification without an image, never a
    notification withheld.
    """
    cur = conn.execute(
        "UPDATE push_handles SET thumbnail = ? WHERE handle = ?", (sqlite3.Binary(jpeg), handle)
    )
    return cur.rowcount > 0


def get_thumbnail(
    conn: sqlite3.Connection, handle: str, *, now: float | None = None
) -> bytes | None:
    """The NSE's `GET /v1/push/thumbnail/{handle}` payload, or None if the
    handle is unknown, expired, or its warm-up didn't land."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT thumbnail FROM push_handles WHERE handle = ? AND expires_at > ?",
        (handle, now),
    ).fetchone()
    if row is None or row["thumbnail"] is None:
        return None
    return bytes(row["thumbnail"])


# -- Snooze / mute (plan §6) -------------------------------------------------


def set_snooze(
    conn: sqlite3.Connection,
    *,
    apns_token: str,
    scope: str,
    until_epoch: float,
    now: float | None = None,
) -> None:
    """Snooze `scope` for one device until `until_epoch`.

    Upsert rather than insert: re-snoozing an already-snoozed scope extends
    (or shortens) it, which is what "Snooze 15m" pressed twice has to mean.
    """
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_snoozes (apns_token, scope, until_epoch, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(apns_token, scope) DO UPDATE SET until_epoch=excluded.until_epoch",
        (apns_token, scope, float(until_epoch), now),
    )


def clear_snooze(conn: sqlite3.Connection, *, apns_token: str, scope: str) -> bool:
    cur = conn.execute(
        "DELETE FROM push_snoozes WHERE apns_token = ? AND scope = ?", (apns_token, scope)
    )
    return cur.rowcount > 0


def active_snoozes(
    conn: sqlite3.Connection, apns_token: str, *, now: float | None = None
) -> set[str]:
    """Scopes currently silenced for this device.

    Expiry is a timestamp comparison, not a scheduled job -- the snooze
    re-enables itself at `until_epoch` with nothing to run and nothing to miss
    if the sidecar was restarted in between.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT scope FROM push_snoozes WHERE apns_token = ? AND until_epoch > ?",
        (apns_token, now),
    ).fetchall()
    return {r["scope"] for r in rows}


def list_snoozes(
    conn: sqlite3.Connection, apns_token: str, *, now: float | None = None
) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT scope, until_epoch FROM push_snoozes "
        "WHERE apns_token = ? AND until_epoch > ? ORDER BY scope",
        (apns_token, now),
    ).fetchall()
    return [{"scope": r["scope"], "until_epoch": r["until_epoch"]} for r in rows]


def prune_expired_snoozes(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_snoozes WHERE until_epoch <= ?", (now,))
    return cur.rowcount


def replace_snoozes(
    conn: sqlite3.Connection, *, apns_token: str, snoozes: list[dict[str, Any]]
) -> None:
    """Set this device's snoozes to exactly `snoozes` (registration §8 field).

    Only called when the client *sent* the key: a registration that omits
    `snoozes` leaves existing ones alone. The app re-registers on every launch
    and on every token reissue, and a launch silently cancelling a snooze the
    user set an hour ago would break the plan's "state that survives app kill"
    non-negotiable in the most confusing way available.
    """
    conn.execute("DELETE FROM push_snoozes WHERE apns_token = ?", (apns_token,))
    now = time.time()
    for item in snoozes:
        scope = str(item.get("scope") or "").strip()
        if not scope:
            continue
        # Plan §8 spells the registration field `until`; the standalone
        # `POST /v1/push/snooze` body spells it `until_epoch`. Both are read
        # here so a client using either vocabulary lands in the same place.
        raw_until = item.get("until", item.get("until_epoch", 0))
        try:
            until = float(raw_until)
        except (TypeError, ValueError):
            continue
        if until > now:
            set_snooze(conn, apns_token=apns_token, scope=scope, until_epoch=until, now=now)


# -- Rate limiting (plan §6) -------------------------------------------------


def count_sends_since(
    conn: sqlite3.Connection, *, apns_token: str, situation_id: str, since: float
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM push_sends "
        "WHERE apns_token = ? AND situation_id = ? AND sent_at > ?",
        (apns_token, situation_id, since),
    ).fetchone()
    return int(row["n"]) if row else 0


def record_send(
    conn: sqlite3.Connection, *, apns_token: str, situation_id: str, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_sends (apns_token, situation_id, sent_at) VALUES (?, ?, ?)",
        (apns_token, situation_id, now),
    )


def bump_suppressed(conn: sqlite3.Connection, *, apns_token: str, situation_id: str) -> None:
    conn.execute(
        "INSERT INTO push_suppressed (apns_token, situation_id, count) VALUES (?, ?, 1) "
        "ON CONFLICT(apns_token, situation_id) DO UPDATE SET count = count + 1",
        (apns_token, situation_id),
    )


def take_suppressed(conn: sqlite3.Connection, *, apns_token: str, situation_id: str) -> int:
    """Read and reset the suppressed counter.

    Read-and-reset in one call because the count exists only to be spent on
    the next push's `" · +X more"` suffix (plan §6) -- leaving it behind would
    repeat the same claim on every subsequent push.
    """
    row = conn.execute(
        "SELECT count FROM push_suppressed WHERE apns_token = ? AND situation_id = ?",
        (apns_token, situation_id),
    ).fetchone()
    count = int(row["count"]) if row else 0
    if count:
        conn.execute(
            "DELETE FROM push_suppressed WHERE apns_token = ? AND situation_id = ?",
            (apns_token, situation_id),
        )
    return count


def prune_old_sends(
    conn: sqlite3.Connection, *, older_than: float, now: float | None = None
) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_sends WHERE sent_at <= ?", (now - older_than,))
    return cur.rowcount


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
