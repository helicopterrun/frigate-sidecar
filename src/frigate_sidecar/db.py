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
    out: dict[str, Any] = {k: row[k] for k in row.keys()}
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
