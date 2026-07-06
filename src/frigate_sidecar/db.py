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

-- BOM builder: build a KiCad-style Master BOM one part at a time. Not
-- Frigate-related; it's a hardware-engineering tool that lives in this sidecar.
-- One `bom_projects` row per board/assembly (the workbook's Build_Config), each
-- owning many `bom_items` line rows. The ~26 fields worth querying are real
-- columns; the rest of the 94-column Master BOM superset rides along in the
-- `extra_fields` JSON blob (see bom_schema.py). Computed columns (quantities,
-- costs, buy qty) are derived on read, never stored.
CREATE TABLE IF NOT EXISTS bom_projects (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                    TEXT NOT NULL UNIQUE,
    project_name            TEXT NOT NULL,
    board_name              TEXT,
    pcb_revision            TEXT,
    bom_revision            TEXT,
    build_quantity          INTEGER NOT NULL DEFAULT 1,
    attrition_pct           REAL NOT NULL DEFAULT 0.05,
    currency                TEXT NOT NULL DEFAULT 'USD',
    assembly_vendor         TEXT,
    assembly_method_default TEXT DEFAULT 'SMT',
    owner                   TEXT,
    source_cad_tool         TEXT DEFAULT 'KiCad',
    notes                   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bom_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            INTEGER NOT NULL REFERENCES bom_projects(id) ON DELETE CASCADE,
    item_no               INTEGER,
    designator            TEXT,
    populate              TEXT DEFAULT 'YES',
    variant               TEXT DEFAULT 'Base',
    qty_per_assembly      REAL DEFAULT 1,
    symbol                TEXT,
    footprint             TEXT,
    part_category         TEXT,
    value                 TEXT,
    description           TEXT,
    package_size          TEXT,
    manufacturer          TEXT,
    mpn                   TEXT DEFAULT 'TBD',
    datasheet_url         TEXT,
    preferred_distributor TEXT,
    preferred_dpn         TEXT DEFAULT 'TBD',
    distributor_url       TEXT,
    do_not_substitute     TEXT DEFAULT 'N',
    lifecycle_status      TEXT DEFAULT 'TBD',
    moq                   INTEGER,
    order_multiple        INTEGER,
    unit_cost             REAL,
    risk_level            TEXT DEFAULT 'Unknown',
    review_status         TEXT DEFAULT 'Needs Review',
    comment               TEXT,
    source                TEXT DEFAULT 'Manual',
    extra_fields          TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bom_items_project ON bom_items(project_id, item_no);
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


def open_sidecar(path: str | Path) -> sqlite3.Connection:
    """Open the sidecar DB read/write, creating directory + schema if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA journal_mode = WAL")
    # Enforce FKs so deleting a bom_projects row cascades to its bom_items.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SIDECAR_SCHEMA)
    conn.commit()
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


def time_window_clause(days: float, column: str = "start_time") -> tuple[str, list[float]]:
    """Build a `<column> >= ?` clause for the last `days` days."""
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


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. `p` in [0, 100]. NaN on empty input."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]
