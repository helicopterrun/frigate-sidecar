"""SQLite helpers for frigate-sidecar.

Two databases are involved:
    1. Frigate's own DB (`frigate.db`) — opened read-only, always.
    2. The sidecar DB (e.g. `frigate-sidecar.db`) — read/write, we own its
       schema.

The pattern is to open Frigate's DB read-only and ATTACH the sidecar so we
can JOIN across them in a single query.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SIDECAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_labels (
    event_id   TEXT PRIMARY KEY,
    label      TEXT NOT NULL CHECK(label IN ('fp','tp','skip')),
    note       TEXT,
    labeled_at TEXT NOT NULL,
    session    TEXT
);
CREATE INDEX IF NOT EXISTS idx_triage_label ON triage_labels(label);

CREATE TABLE IF NOT EXISTS face_attempts (
    filename        TEXT PRIMARY KEY,
    event_id        TEXT,
    frame_ts        REAL,
    recognized_name TEXT,
    recog_score     REAL,
    sharpness       REAL,
    area_px         INTEGER,
    quality_score   REAL,
    decision        TEXT,
    assigned_name   TEXT,
    scored_at       TEXT,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_face_decision ON face_attempts(decision);
CREATE INDEX IF NOT EXISTS idx_face_quality ON face_attempts(quality_score);

-- Toybox: arcade-style high scores for the in-house games (50-states quiz, etc).
-- Not Frigate-related; it's a for-fun page. `game` namespaces the leaderboard so
-- a future game can share the table.
CREATE TABLE IF NOT EXISTS toybox_scores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    game      TEXT NOT NULL,
    name      TEXT NOT NULL,
    score     INTEGER NOT NULL,
    played_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toybox_board ON toybox_scores(game, score DESC);

-- Seed one example high score so a fresh board isn't empty (classic arcade vibe).
-- Guarded so it only appears while the board has no real entries yet.
INSERT INTO toybox_scores (game, name, score, played_at)
SELECT 'states50', 'BOB1', 30, '2026-06-05T00:00:00'
WHERE NOT EXISTS (SELECT 1 FROM toybox_scores WHERE game = 'states50');

-- Scrub-cache: uniform-cadence sprite sheets (docs/scrub-cache-and-proxy-spec.md).
-- `interval_s` is a hard contract -- every frame in [start_ts, end_ts) exists
-- within interval_s/2 of start_ts + n*interval_s, or the bucket is split.
CREATE TABLE IF NOT EXISTS scrub_buckets (
    camera            TEXT NOT NULL,
    start_ts          REAL NOT NULL,        -- inclusive
    end_ts            REAL NOT NULL,        -- exclusive; grows as the live bucket fills
    interval_s        REAL NOT NULL,        -- the hard-contract cadence
    width             INTEGER NOT NULL,
    height            INTEGER NOT NULL,
    generated_through REAL NOT NULL,        -- newest moment with a frame behind it
    complete          INTEGER NOT NULL DEFAULT 0,  -- 1 once end_ts is final & immutable
    PRIMARY KEY (camera, start_ts, interval_s)
);
CREATE INDEX IF NOT EXISTS idx_scrub_bucket_cam ON scrub_buckets(camera, start_ts);

-- One row per sprite-sheet image. `count` is filled cells (< cols*rows while
-- still filling); it's part of the sheet's URL/filename so the object is
-- immutable at every version (docs spec §4.3 -- a growing count must never
-- reuse a URL).
CREATE TABLE IF NOT EXISTS scrub_sheets (
    camera     TEXT NOT NULL,
    start_ts   REAL NOT NULL,               -- sheet's first cell wall-clock time
    interval_s REAL NOT NULL,
    cols       INTEGER NOT NULL,
    rows       INTEGER NOT NULL,
    cell_w     INTEGER NOT NULL,
    cell_h     INTEGER NOT NULL,
    count      INTEGER NOT NULL,            -- filled cells
    path       TEXT NOT NULL,               -- on-disk relative path under scrub.cache_dir
    complete   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera, start_ts, interval_s, count)
);
CREATE INDEX IF NOT EXISTS idx_scrub_sheet_cam ON scrub_sheets(camera, start_ts);
"""


def open_frigate_ro(path: str | Path) -> sqlite3.Connection:
    """Open Frigate's DB read-only. Raises FileNotFoundError if missing."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Frigate DB not found: {p}")
    # `mode=ro` on the URI already enforces read-only for main; do NOT set
    # PRAGMA query_only here because it's a connection-level flag and would
    # also block writes against any DB ATTACHed later (e.g. the sidecar).
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


# Databases whose schema this process has already applied. Every request opens
# its own connection, and replaying ~15 DDL statements plus the seed INSERT on
# each one is pure overhead once the file exists.
_SCHEMA_APPLIED: set[str] = set()


def open_sidecar(path: str | Path) -> sqlite3.Connection:
    """Open the sidecar DB read/write, creating directory + schema if needed."""
    p = Path(path)
    key = str(p.resolve())
    needs_schema = key not in _SCHEMA_APPLIED or not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA journal_mode = WAL")
    if needs_schema:
        conn.executescript(SIDECAR_SCHEMA)
        conn.commit()
        _SCHEMA_APPLIED.add(key)
    return conn


def open_joined(
    frigate_path: str | Path,
    sidecar_path: str | Path,
    sidecar_alias: str = "sidecar",
) -> sqlite3.Connection:
    """Open Frigate read-only with the sidecar ATTACHed under `sidecar_alias`.

    Ensures the sidecar exists (with schema) before attaching. The attached
    sidecar is opened in the default mode by SQLite (rw), so writes are
    allowed against it through the joined handle.
    """
    sp = Path(sidecar_path)
    if not sp.exists():
        # Initialize the sidecar so ATTACH succeeds.
        open_sidecar(sp).close()

    conn = open_frigate_ro(frigate_path)
    # ATTACH uses a separate connection internally; the read-only PRAGMA on
    # `main` doesn't propagate to the attached DB.
    conn.execute(f"ATTACH DATABASE ? AS {sidecar_alias}", (str(sp),))
    return conn


# Publish lag: how far behind wall-clock a segment's end_time typically is once
# it's committed. Measured live on the current deployment (2026-07-30): 6.2s on
# both alley-wide and doorbell, within the 4-10s range client-side review
# estimated. Used to compute `authoritative_through` (docs spec §4.4 finding 4)
# -- a field that must keep advancing at wall-clock rate even if a camera goes
# silent, unlike `latest_segment_end` (= MAX(end_time)), which freezes on outage.
DEFAULT_PUBLISH_LAG_S = 6.2

# Gap below which two recorded intervals are one interval. Consecutive Frigate
# segments don't abut exactly -- measured live on `street`, 2052 of 2063 seams
# are under 0.1s with a median of 3.3ms, while genuine discontinuities are over
# 1s. An exact-adjacency join therefore never fired and §4.4's "merged
# intervals, not raw segments" shipped as raw segments: 2064 intervals over six
# hours where ~15 describe the same coverage. 0.25s sits an order of magnitude
# above the seams, an order below the real gaps, and well below the finest row
# a client draws.
DEFAULT_MERGE_TOLERANCE_S = 0.25


def recording_coverage(
    conn: sqlite3.Connection,
    camera: str,
    start: float,
    end: float,
    *,
    now: float,
    publish_lag_s: float = DEFAULT_PUBLISH_LAG_S,
    merge_tolerance_s: float = DEFAULT_MERGE_TOLERANCE_S,
) -> dict[str, Any]:
    """Merged recorded intervals for `camera` in [start, end), plus the two
    distinct "how far can I trust this" fields (docs spec §4.4).

    `latest_segment_end` is diagnostic only (freezes if the camera goes
    offline). `authoritative_through` is what gates client coverage claims --
    it keeps advancing at wall-clock rate regardless of camera health, so its
    divergence from `latest_segment_end` IS the outage signal.
    """
    rows = conn.execute(
        "SELECT start_time, end_time FROM recordings "
        "WHERE camera = ? AND start_time < ? AND end_time > ? "
        "ORDER BY start_time",
        (camera, end, start),
    ).fetchall()

    merged: list[list[float]] = []
    for row in rows:
        seg_start = max(row["start_time"], start)
        seg_end = min(row["end_time"], end)
        if seg_end <= seg_start:
            continue
        # Tolerance, not exact adjacency: segment boundaries are milliseconds
        # apart, so `seg_start <= merged[-1][1]` essentially never fired and
        # every segment came back as its own interval.
        if merged and seg_start <= merged[-1][1] + merge_tolerance_s:
            merged[-1][1] = max(merged[-1][1], seg_end)
        else:
            merged.append([seg_start, seg_end])

    latest_row = conn.execute(
        "SELECT MAX(end_time) AS latest FROM recordings WHERE camera = ?", (camera,)
    ).fetchone()
    latest_segment_end = None
    if latest_row and latest_row["latest"] is not None:
        latest_segment_end = latest_row["latest"]

    return {
        "camera": camera,
        "queried": [start, end],
        "recorded": [tuple(interval) for interval in merged],
        "latest_segment_end": latest_segment_end,
        "authoritative_through": now - publish_lag_s,
    }


def parse_event_data(row: sqlite3.Row) -> dict[str, Any]:
    """Flatten an event row's `data` JSON blob.

    Frigate stores score/top_score/box/region nested under `data`. This
    keeps the row's columns and adds `data_*` keys for the parsed fields.
    """
    out: dict[str, Any] = {k: row[k] for k in row.keys()}  # noqa: SIM118 (sqlite3.Row needs .keys())
    raw = out.get("data")
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    out["data_score"] = parsed.get("score")
    out["data_top_score"] = parsed.get("top_score")
    out["data_box"] = parsed.get("box")
    out["data_region"] = parsed.get("region")
    out["data_type"] = parsed.get("type")
    out["data_attributes"] = parsed.get("attributes")
    out["_data_parsed"] = parsed
    return out


def time_window_clause(days: float, column: str = "start_time") -> tuple[str, list[Any]]:
    """Build a `<column> >= ?` clause for the last `days` days.

    The returned params list is the caller's to extend with further bound
    values (camera, label, ...), so it is deliberately not float-only.
    """
    cutoff = time.time() - days * 86400
    return f"{column} >= ?", [cutoff]


def fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def upsert_scrub_bucket(
    conn: sqlite3.Connection,
    *,
    camera: str,
    start_ts: float,
    end_ts: float,
    interval_s: float,
    width: int,
    height: int,
    generated_through: float,
    complete: bool,
) -> None:
    conn.execute(
        "INSERT INTO scrub_buckets "
        "(camera, start_ts, end_ts, interval_s, width, height, generated_through, complete) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(camera, start_ts, interval_s) DO UPDATE SET "
        "end_ts=excluded.end_ts, generated_through=excluded.generated_through, "
        "complete=excluded.complete",
        (camera, start_ts, end_ts, interval_s, width, height, generated_through, int(complete)),
    )


def list_scrub_buckets(
    conn: sqlite3.Connection, camera: str, start: float, end: float
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM scrub_buckets WHERE camera = ? AND start_ts < ? AND end_ts > ? "
        "ORDER BY start_ts",
        (camera, end, start),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_generated_through(
    conn: sqlite3.Connection, camera: str, interval_s: float | None = None
) -> float | None:
    """Newest `generated_through` across this camera's buckets.

    When `interval_s` is given, restricts to that tier's buckets only -- each
    thinning tier (§5.5) tracks its own resume point independently, since a
    single camera can have both a recent- and an aged-tier bucket in flight
    at once.
    """
    if interval_s is None:
        row = conn.execute(
            "SELECT MAX(generated_through) AS g FROM scrub_buckets WHERE camera = ?", (camera,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(generated_through) AS g FROM scrub_buckets "
            "WHERE camera = ? AND interval_s = ?",
            (camera, interval_s),
        ).fetchone()
    return row["g"] if row and row["g"] is not None else None


def delete_scrub_buckets_before(conn: sqlite3.Connection, camera: str | None, cutoff: float) -> int:
    if camera is None:
        cur = conn.execute("DELETE FROM scrub_buckets WHERE end_ts < ?", (cutoff,))
    else:
        cur = conn.execute(
            "DELETE FROM scrub_buckets WHERE camera = ? AND end_ts < ?", (camera, cutoff)
        )
    return cur.rowcount


def upsert_scrub_sheet(
    conn: sqlite3.Connection,
    *,
    camera: str,
    start_ts: float,
    interval_s: float,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    count: int,
    path: str,
    complete: bool,
) -> None:
    conn.execute(
        "INSERT INTO scrub_sheets "
        "(camera, start_ts, interval_s, cols, rows, cell_w, cell_h, count, path, complete) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(camera, start_ts, interval_s, count) DO UPDATE SET "
        "path=excluded.path, complete=excluded.complete",
        (camera, start_ts, interval_s, cols, rows, cell_w, cell_h, count, path, int(complete)),
    )


def list_scrub_sheets(
    conn: sqlite3.Connection, camera: str, start: float, end: float
) -> list[dict[str, Any]]:
    """Latest published version of each sheet intersecting [start, end).

    Older (superseded) counts stay in the table as immutable objects (their
    URLs remain servable forever, §4.3) but /sheets only advertises the
    current one per (camera, interval_s, start_ts) -- otherwise a client
    listing sheets would see multiple candidate URLs for the same instant
    with no way to tell which is current.
    """
    rows_ = conn.execute(
        """
        SELECT s.* FROM scrub_sheets s
        JOIN (
            SELECT camera, interval_s, start_ts, MAX(count) AS max_count
            FROM scrub_sheets
            WHERE camera = ? AND start_ts < ? AND (start_ts + cols * rows * interval_s) > ?
            GROUP BY camera, interval_s, start_ts
        ) latest
        ON s.camera = latest.camera AND s.interval_s = latest.interval_s
           AND s.start_ts = latest.start_ts AND s.count = latest.max_count
        WHERE s.camera = ?
        ORDER BY s.start_ts
        """,
        (camera, end, start, camera),
    ).fetchall()
    return [dict(r) for r in rows_]


def get_scrub_sheet(
    conn: sqlite3.Connection, camera: str, start_ts: float, interval_s: float, count: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM scrub_sheets WHERE camera = ? AND start_ts = ? AND interval_s = ? "
        "AND count = ?",
        (camera, start_ts, interval_s, count),
    ).fetchone()
    return dict(row) if row else None


def delete_scrub_sheets_before(
    camera: str | None, cutoff: float, conn: sqlite3.Connection
) -> list[str]:
    """Delete sheet rows ending before `cutoff`, returning their on-disk paths
    so the caller can unlink the files too (mirrors wildlife.py's
    mtime-bounded eviction, but keyed on content time here)."""
    if camera is None:
        rows = conn.execute(
            "SELECT path FROM scrub_sheets WHERE (start_ts + cols * rows * interval_s) < ?",
            (cutoff,),
        ).fetchall()
        conn.execute(
            "DELETE FROM scrub_sheets WHERE (start_ts + cols * rows * interval_s) < ?", (cutoff,)
        )
    else:
        rows = conn.execute(
            "SELECT path FROM scrub_sheets WHERE camera = ? AND "
            "(start_ts + cols * rows * interval_s) < ?",
            (camera, cutoff),
        ).fetchall()
        conn.execute(
            "DELETE FROM scrub_sheets WHERE camera = ? AND "
            "(start_ts + cols * rows * interval_s) < ?",
            (camera, cutoff),
        )
    return [r["path"] for r in rows]


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. `p` in [0, 100]. NaN on empty input."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]
