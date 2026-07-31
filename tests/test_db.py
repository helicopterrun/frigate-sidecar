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


def test_recording_coverage_merges_across_segment_seams(tmp_path: Path) -> None:
    """§4.4 promises merged intervals, not raw segments.

    Consecutive Frigate segments don't abut exactly -- measured live, 2052 of
    2063 seams on `street` were under 0.1s (median 3.3ms) -- so an
    exact-adjacency join never fired and six hours came back as 2064 intervals
    describing what ~15 do. Real discontinuities are over a second.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, path TEXT, "
        "start_time REAL, end_time REAL);"
    )
    base = 1_800_000_000.0
    rows = []
    t = base
    for i in range(60):
        rows.append((f"s{i}", "street", "/x.mp4", t, t + 10.0))
        # Millisecond seam, as real segments have.
        t += 10.0033
    # One genuine outage: a 5 minute hole.
    t += 300.0
    for i in range(60, 90):
        rows.append((f"s{i}", "street", "/x.mp4", t, t + 10.0))
        t += 10.0033
    conn.executemany("INSERT INTO recordings VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    ro = db.open_frigate_ro(frigate_db)
    try:
        result = db.recording_coverage(ro, "street", base, t + 10, now=t + 20)
    finally:
        ro.close()

    recorded = result["recorded"]
    assert len(recorded) == 2, f"expected 2 intervals around the outage, got {len(recorded)}"
    assert recorded[1][0] - recorded[0][1] > 250, "the real outage must survive the merge"


def test_recording_coverage_keeps_gaps_above_the_tolerance(tmp_path: Path) -> None:
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, path TEXT, "
        "start_time REAL, end_time REAL);"
    )
    base = 1_800_000_000.0
    conn.executemany(
        "INSERT INTO recordings VALUES (?, ?, ?, ?, ?)",
        [
            ("a", "street", "/x.mp4", base, base + 10.0),
            # 0.5s: above the 0.25s tolerance, so a distinct interval.
            ("b", "street", "/x.mp4", base + 10.5, base + 20.0),
        ],
    )
    conn.commit()
    conn.close()

    ro = db.open_frigate_ro(frigate_db)
    try:
        result = db.recording_coverage(ro, "street", base, base + 30, now=base + 30)
    finally:
        ro.close()
    assert len(result["recorded"]) == 2
