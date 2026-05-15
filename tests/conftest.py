"""Pytest fixtures shared across the sidecar test suite."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Approximation of Frigate's `event` table — enough columns for our queries.
FRIGATE_EVENT_SCHEMA = """
CREATE TABLE event (
    id           TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    sub_label    TEXT,
    start_time   REAL NOT NULL,
    end_time     REAL,
    has_clip     INTEGER NOT NULL DEFAULT 0,
    has_snapshot INTEGER NOT NULL DEFAULT 0,
    score        REAL,
    top_score    REAL,
    zones        TEXT,
    data         TEXT,
    false_positive INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.fixture
def frigate_db_path(tmp_path: Path) -> Path:
    """A minimal stand-in for Frigate's DB with a handful of seeded events."""
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(FRIGATE_EVENT_SCHEMA)
    now = time.time()

    rows = [
        # id, camera, label, start_time delta-seconds, score, zones, top_score, has_clip, has_snapshot
        ("e1", "alley-overview", "person", -300, 0.92, "parking_area_people", 0.94, 1, 1),
        ("e2", "alley-overview", "person", -600, 0.61, "alley", 0.65, 1, 1),
        ("e3", "alley-east", "dog", -900, 0.78, "back_walkway", 0.80, 1, 1),
        ("e4", "street-overview", "car", -1200, 0.55, "49th_street", 0.58, 0, 1),
        ("e5", "alley-overview", "person", -86400 * 30, 0.72, "alley", 0.75, 1, 1),  # old
    ]
    for eid, cam, label, dt, score, zones, top, has_clip, has_snap in rows:
        data = json.dumps(
            {"score": score, "top_score": top, "box": [0.1, 0.2, 0.3, 0.4], "type": "object"}
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, "
            "zones, data, has_clip, has_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, cam, label, now + dt, now + dt + 30, score, top, zones, data, has_clip, has_snap),
        )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def sidecar_db_path(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "frigate-sidecar.db"
