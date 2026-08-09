"""SQLite-backed storage for `cards.Card` (Elsinore Phase 2: delivery
pipeline).

Same shape as `push/store.py`: plain functions over an already-open
`sqlite3.Connection`, one table (`push_cards`, in `db.SIDECAR_SCHEMA`), no
ORM. `cards.py` stays pure and DB-free; this module is the only place that
turns a `Card` into rows and back.
"""

from __future__ import annotations

import sqlite3

from frigate_sidecar.push.cards import Card


def _row_to_card(row: sqlite3.Row) -> Card:
    try:
        peak_level = row["peak_level"]
    except (IndexError, KeyError):
        peak_level = row["level"]
    return Card(
        card_key=row["card_key"],
        level=row["level"],
        peak_level=peak_level or row["level"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state_since_at=row["state_since_at"],
        sound_count=row["sound_count"],
        handled=bool(row["handled"]),
        handled_at=row["handled_at"],
        last_sound_at=row["last_sound_at"],
        resound_count=row["resound_count"],
        resolved=bool(row["resolved"]),
        closed=bool(row["closed"]),
    )


def get_card(conn: sqlite3.Connection, card_key: str) -> Card | None:
    row = conn.execute(
        "SELECT * FROM push_cards WHERE card_key = ?", (card_key,)
    ).fetchone()
    return _row_to_card(row) if row is not None else None


def upsert_card(
    conn: sqlite3.Connection,
    card: Card,
    *,
    subject_kind: str = "",
    place_class: str = "",
    camera: str = "",
    zone_name: str = "",
) -> None:
    """Insert or fully overwrite the row for `card.card_key`.

    Unlike `store.upsert_device` (an idempotent PUT that must preserve
    fields the caller didn't send), a card's row is always written by the
    single delivery pipeline that owns its full state -- there is nothing to
    merge, so this is a plain replace.
    """
    conn.execute(
        "INSERT INTO push_cards "
        "(card_key, level, peak_level, subject_kind, place_class, camera, zone_name, "
        " created_at, updated_at, state_since_at, sound_count, handled, handled_at, "
        " last_sound_at, resound_count, resolved, closed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(card_key) DO UPDATE SET "
        "level=excluded.level, peak_level=excluded.peak_level, "
        "subject_kind=excluded.subject_kind, "
        "place_class=excluded.place_class, camera=excluded.camera, "
        "zone_name=excluded.zone_name, updated_at=excluded.updated_at, "
        "state_since_at=excluded.state_since_at, "
        "sound_count=excluded.sound_count, handled=excluded.handled, "
        "handled_at=excluded.handled_at, last_sound_at=excluded.last_sound_at, "
        "resound_count=excluded.resound_count, resolved=excluded.resolved, "
        "closed=excluded.closed",
        (
            card.card_key, card.level, card.peak_level, subject_kind, place_class,
            camera, zone_name,
            card.created_at, card.updated_at, card.state_since_at, card.sound_count,
            int(card.handled), card.handled_at, card.last_sound_at, card.resound_count,
            int(card.resolved), int(card.closed),
        ),
    )
    conn.commit()


def mark_handled(conn: sqlite3.Connection, card_key: str, *, now: float) -> None:
    conn.execute(
        "UPDATE push_cards SET handled = 1, handled_at = ? WHERE card_key = ?",
        (now, card_key),
    )
    conn.commit()


def migrate_drop_zone_from_card_keys(conn: sqlite3.Connection) -> int:
    """One-off, idempotent migration: collapse rows written under the old
    `{camera}:{zone}:{subject_kind}:{subject_id}` card-key scheme onto the
    current `{camera}:{subject_kind}:{subject_id}` identity
    (`delivery.build_card_key`'s docstring has the live-observed failure
    that motivated dropping zone from identity).

    Old-format rows are recognized structurally: exactly 4 `:`-separated
    components (`str.split(":", 3)` so a subject id containing `:` stays
    intact as the 4th piece). System cards (`{camera}:system:{reason}`,
    3 components) were never zone-bearing and are left untouched, as are
    rows already in the 3-component post-fix shape.

    For each old-format key, dropping the zone component maps it onto a new
    key that may collide with other old-format rows for the same subject
    (this is exactly the fragmentation bug) and/or with a row a newer
    delivery already wrote under the fixed scheme. All contributors to a
    given new key are compared by `(updated_at, created_at)`; the newest
    wins and becomes that key's row. Every old-format row is then marked
    `resolved`/`closed` so none linger as phantom open cards -- including
    the winner's own old-format row, since its state now lives under the
    new key instead.

    Returns the number of old-format rows collapsed. Safe to call on every
    startup: a database with nothing in the old shape touches zero rows.
    """
    rows = conn.execute("SELECT * FROM push_cards").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        # Already migrated (or a naturally-resolved old-format card that
        # never needed collapsing in the first place) -- old-format rows are
        # never deleted, only closed, so without this check every startup
        # would re-"collapse" the same rows forever and log a stale count.
        if row["closed"] and row["resolved"]:
            continue
        parts = row["card_key"].split(":", 3)
        if len(parts) == 4:
            camera, _zone, subject_kind, subject_id = parts
            new_key = f"{camera}:{subject_kind}:{subject_id}"
            groups.setdefault(new_key, []).append(row)

    collapsed = 0
    for new_key, old_rows in groups.items():
        existing_new = conn.execute(
            "SELECT * FROM push_cards WHERE card_key = ?", (new_key,)
        ).fetchone()
        candidates = list(old_rows) + ([existing_new] if existing_new is not None else [])
        winner = max(candidates, key=lambda r: (r["updated_at"], r["created_at"]))

        try:
            peak_level = winner["peak_level"]
        except (IndexError, KeyError):
            peak_level = winner["level"]
        conn.execute(
            "INSERT INTO push_cards "
            "(card_key, level, peak_level, subject_kind, place_class, camera, zone_name, "
            " created_at, updated_at, state_since_at, sound_count, handled, handled_at, "
            " last_sound_at, resound_count, resolved, closed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(card_key) DO UPDATE SET "
            "level=excluded.level, peak_level=excluded.peak_level, "
            "subject_kind=excluded.subject_kind, "
            "place_class=excluded.place_class, camera=excluded.camera, "
            "zone_name=excluded.zone_name, created_at=excluded.created_at, "
            "updated_at=excluded.updated_at, state_since_at=excluded.state_since_at, "
            "sound_count=excluded.sound_count, handled=excluded.handled, "
            "handled_at=excluded.handled_at, last_sound_at=excluded.last_sound_at, "
            "resound_count=excluded.resound_count, resolved=excluded.resolved, "
            "closed=excluded.closed",
            (
                new_key, winner["level"], peak_level or winner["level"],
                winner["subject_kind"], winner["place_class"],
                winner["camera"], winner["zone_name"], winner["created_at"], winner["updated_at"],
                winner["state_since_at"], winner["sound_count"], winner["handled"],
                winner["handled_at"], winner["last_sound_at"], winner["resound_count"],
                winner["resolved"], winner["closed"],
            ),
        )
        for row in old_rows:
            conn.execute(
                "UPDATE push_cards SET resolved = 1, closed = 1, updated_at = ? "
                "WHERE card_key = ?",
                (winner["updated_at"], row["card_key"]),
            )
            collapsed += 1
    conn.commit()
    return collapsed


def get_card_context(conn: sqlite3.Connection, card_key: str) -> dict[str, str] | None:
    """The copy context (`subject_kind`/`place_class`/`camera`/`zone_name`)
    stored alongside `card_key`'s row -- `None` if the card doesn't exist.
    Used by the cross-camera dedup path to recover the *merged* card's
    original camera, since a card that keeps its identity when a second
    camera starts contributing must also keep reporting the camera that
    first created it (`docs/push-notifications.md` "Cross-camera
    deduplication")."""
    row = conn.execute(
        "SELECT subject_kind, place_class, camera, zone_name FROM push_cards "
        "WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def find_dedup_candidate(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    zone_name: str,
    exclude_key: str,
    now: float,
    window_s: float,
) -> str | None:
    """The oldest open card sharing `subject_kind`/`zone_name` and created
    within `window_s` of `now` -- the cross-camera dedup candidate for a
    *fresh* track (caller only calls this when its own card doesn't exist
    yet). Oldest, not newest: with three cameras sharing a zone, the first
    one's card is the one every later camera should merge onto, not
    whichever alias happened to be looked up last.

    `exclude_key` guards against a card matching itself; in practice the
    caller's own key can't have a row yet (that's the precondition for
    calling this at all), but the check is free and cheap insurance against
    a future caller getting that precondition wrong.
    """
    row = conn.execute(
        "SELECT card_key FROM push_cards "
        "WHERE subject_kind = ? AND zone_name = ? AND closed = 0 AND resolved = 0 "
        "AND card_key != ? AND created_at >= ? "
        "ORDER BY created_at ASC LIMIT 1",
        (subject_kind, zone_name, exclude_key, now - window_s),
    ).fetchone()
    return row["card_key"] if row is not None else None


def get_track_alias(conn: sqlite3.Connection, camera: str, track_id: str) -> str | None:
    row = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE camera = ? AND track_id = ?",
        (camera, track_id),
    ).fetchone()
    return row["card_key"] if row is not None else None


def set_track_alias(
    conn: sqlite3.Connection, camera: str, track_id: str, card_key: str, now: float
) -> None:
    conn.execute(
        "INSERT INTO push_card_track_aliases (camera, track_id, card_key, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(camera, track_id) DO UPDATE SET "
        "card_key=excluded.card_key, created_at=excluded.created_at",
        (camera, track_id, card_key, now),
    )
    conn.commit()


def delete_track_alias(conn: sqlite3.Connection, camera: str, track_id: str) -> None:
    conn.execute(
        "DELETE FROM push_card_track_aliases WHERE camera = ? AND track_id = ?",
        (camera, track_id),
    )
    conn.commit()


def list_open_urgent_cards(conn: sqlite3.Connection) -> list[tuple[Card, dict[str, str]]]:
    """Open (`closed = 0`, `resolved = 0`) `urgent` cards -- the candidate
    set the re-sound sweep checks against its timer. Paired with the copy
    context (`subject_kind`/`place_class`/`camera`/`zone_name`) since
    `Card` itself doesn't carry it and the sweep has no live event to
    re-derive it from -- only what was stored at the card's last mutation.
    """
    rows = conn.execute(
        "SELECT * FROM push_cards WHERE level = 'urgent' AND closed = 0 AND resolved = 0"
    ).fetchall()
    return [
        (
            _row_to_card(row),
            {
                "subject_kind": row["subject_kind"],
                "place_class": row["place_class"],
                "camera": row["camera"],
                "zone_name": row["zone_name"],
            },
        )
        for row in rows
    ]
