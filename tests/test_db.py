from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from frigate_sidecar import db


def test_open_frigate_ro_rejects_writes(frigate_db_path: Path) -> None:
    conn = db.open_frigate_ro(frigate_db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO event (id, camera, label, start_time) VALUES ('x','c','l',0)")


def test_open_sidecar_creates_schema(sidecar_db_path: Path) -> None:
    assert not sidecar_db_path.exists()
    conn = db.open_sidecar(sidecar_db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(triage_labels)")}
    assert cols == {"event_id", "label", "note", "labeled_at", "session"}
    conn.close()


def test_open_sidecar_idempotent(sidecar_db_path: Path) -> None:
    db.open_sidecar(sidecar_db_path).close()
    db.open_sidecar(sidecar_db_path).close()
    conn = sqlite3.connect(sidecar_db_path)
    n = conn.execute("SELECT COUNT(*) FROM triage_labels").fetchone()[0]
    assert n == 0


def test_open_joined_round_trip(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    conn = db.open_joined(frigate_db_path, sidecar_db_path)
    # Insert a triage label via the joined handle.
    conn.execute(
        "INSERT INTO sidecar.triage_labels (event_id, label, labeled_at, session) "
        "VALUES (?, ?, ?, ?)",
        ("e1", "tp", "2026-05-15T12:00:00Z", "test"),
    )
    conn.commit()

    # Join Frigate's events with our labels.
    rows = conn.execute(
        """
        SELECT e.id, e.camera, t.label
          FROM main.event e
          LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
         WHERE e.id = ?
        """,
        ("e1",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["camera"] == "alley-overview"
    assert rows[0]["label"] == "tp"


def test_parse_event_data(frigate_db_path: Path) -> None:
    conn = db.open_frigate_ro(frigate_db_path)
    row = conn.execute("SELECT * FROM event WHERE id = 'e1'").fetchone()
    parsed = db.parse_event_data(row)
    assert parsed["data_score"] == 0.92
    assert parsed["data_top_score"] == 0.94
    assert parsed["data_box"] == [0.1, 0.2, 0.3, 0.4]
    assert parsed["data_type"] == "object"


def test_time_window_clause() -> None:
    sql, params = db.time_window_clause(7)
    assert sql == "start_time >= ?"
    assert len(params) == 1
    assert time.time() - params[0] == pytest.approx(7 * 86400, abs=2)


def test_percentile_basics() -> None:
    assert db.percentile([], 50) != db.percentile([], 50)  # NaN
    assert db.percentile([1.0], 0) == 1.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0) == 1.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
