"""Notability stamping through the store (`recompute`'s `stamper`), the
migration that adds the columns, and `marcellus encounters restamp`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from marcellus import db
from marcellus.cli import app
from marcellus.encounters import notability, repair, store
from marcellus.encounters.linker import Atom, LinkDecision, LinkerConfig
from marcellus.encounters.notability import Stamp

NOON = 1_782_150_000.0  # arbitrary; the fake stampers below ignore the clock

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)

STAMP_COLS = "tag, place, outcome, tag_reason"


def _atom(atom_id: str, *, camera: str = "cam-a", start: float = NOON, **kw: Any) -> Atom:
    fields: dict[str, Any] = dict(
        end_time=start + 5.0,
        labels=("person",),
        zones=(),
        event_ids=(f"ev-{atom_id}",),
        sub_labels=(),
        severity="detection",
    )
    fields.update(kw)
    return Atom(atom_id=atom_id, camera=camera, start_time=start, **fields)


def _stamp_cols(conn: sqlite3.Connection, encounter_id: str) -> tuple[Any, ...]:
    row = conn.execute(f"SELECT {STAMP_COLS} FROM encounters WHERE id = ?", (encounter_id,))
    return tuple(row.fetchone())


def _fake(tag: str = "notable", reason: str = "test") -> notability.Stamper:
    calls: list[list[dict[str, Any]]] = []

    def stamper(members: list[dict[str, Any]]) -> Stamp:
        calls.append(members)
        return Stamp(tag, "yard", "glance", reason)

    stamper.calls = calls  # type: ignore[attr-defined]
    return stamper


@pytest.fixture
def conn(sidecar_db_path: Path) -> Any:
    c = db.open_sidecar(sidecar_db_path)
    try:
        yield c
    finally:
        c.close()


def test_new_columns_exist_and_start_null(conn: sqlite3.Connection) -> None:
    enc = store.upsert_atom(conn, _atom("a1"), LinkDecision(None, "new", 1.0), NOON)
    assert _stamp_cols(conn, enc) == (None, None, None, None)


def test_recompute_with_stamper_writes_the_four_columns(conn: sqlite3.Connection) -> None:
    stamper = _fake()
    enc = store.upsert_atom(
        conn, _atom("a1", labels=("person", "dog")), LinkDecision(None, "new", 1.0), NOON,
        stamper=stamper,
    )
    assert _stamp_cols(conn, enc) == ("notable", "yard", "glance", "test")
    (members,) = stamper.calls  # type: ignore[attr-defined]
    assert members[0]["labels"] == ["person", "dog"]  # decoded lists, not raw JSON
    assert members[0]["camera"] == "cam-a"


def test_recompute_without_stamper_leaves_stamp_alone(conn: sqlite3.Connection) -> None:
    enc = store.upsert_atom(
        conn, _atom("a1"), LinkDecision(None, "new", 1.0), NOON, stamper=_fake("background", "x")
    )
    store.upsert_atom(conn, _atom("a2", camera="cam-b"), LinkDecision(enc, "adjacent", 0.9), NOON)
    row = conn.execute("SELECT atom_count FROM encounters WHERE id = ?", (enc,)).fetchone()
    assert row["atom_count"] == 2  # aggregates did update
    assert _stamp_cols(conn, enc) == ("background", "yard", "glance", "x")


def test_restamps_when_members_change(conn: sqlite3.Connection) -> None:
    real = lambda ms: notability.stamp(  # noqa: E731
        ms, zone_classes={}, linger_s=60.0, latitude=47.6, longitude=-122.3
    )
    enc = store.upsert_atom(
        conn, _atom("a1", start=1_782_165_600.0), LinkDecision(None, "new", 1.0), NOON,
        stamper=real,
    )
    assert _stamp_cols(conn, enc)[0] == "background"
    store.upsert_atom(
        conn, _atom("a2", camera="cam-b", start=1_782_165_700.0),
        LinkDecision(enc, "adjacent", 0.9), NOON, stamper=real,
    )
    assert _stamp_cols(conn, enc)[::3] == ("notable", "multi_camera")


def test_seal_stale_never_restamps(conn: sqlite3.Connection) -> None:
    enc = store.upsert_atom(
        conn, _atom("a1"), LinkDecision(None, "new", 1.0), NOON, stamper=_fake("background", "x")
    )
    before = _stamp_cols(conn, enc)
    assert store.seal_stale(conn, NOON + 10 * 86400, CFG) == 1
    assert conn.execute("SELECT sealed_at FROM encounters").fetchone()["sealed_at"] is not None
    assert _stamp_cols(conn, enc) == before


def test_split_pin_merge_and_remove_restamp_both_sides(conn: sqlite3.Connection) -> None:
    stamper = _fake("background", "first")
    e1 = store.upsert_atom(conn, _atom("a1"), LinkDecision(None, "new", 1.0), NOON, stamper=stamper)
    store.upsert_atom(
        conn, _atom("a2", start=NOON + 1), LinkDecision(e1, "same_camera", 0.9), NOON,
        stamper=stamper,
    )
    e2 = store.upsert_atom(
        conn, _atom("b1", camera="cam-z"), LinkDecision(None, "new", 1.0), NOON, stamper=stamper
    )
    conn.commit()

    later = _fake("notable", "later")
    new_id = store.split_atom(conn, "a2", NOON, stamper=later)
    assert _stamp_cols(conn, new_id)[::3] == ("notable", "later")
    assert _stamp_cols(conn, e1)[::3] == ("notable", "later")  # donor too

    third = _fake("background", "third")
    store.pin_atom(conn, "a2", e2, NOON, stamper=third)
    assert _stamp_cols(conn, e2)[::3] == ("background", "third")

    fourth = _fake("notable", "fourth")
    store.merge_encounters(conn, e2, e1, NOON, stamper=fourth)
    assert _stamp_cols(conn, e1)[::3] == ("notable", "fourth")

    fifth = _fake("background", "fifth")
    store.remove_member(conn, "a2", NOON, stamper=fifth)
    assert _stamp_cols(conn, e1)[::3] == ("background", "fifth")


def test_migration_adds_columns_to_old_shape_db() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE encounters (id TEXT PRIMARY KEY, start_time REAL NOT NULL, "
        "end_time REAL, sealed_at REAL, cameras_json TEXT NOT NULL DEFAULT '[]', "
        "labels_json TEXT NOT NULL DEFAULT '[]', identities_json TEXT NOT NULL DEFAULT '[]', "
        "zones_json TEXT NOT NULL DEFAULT '[]', primary_event_id TEXT, "
        "peak_severity TEXT NOT NULL DEFAULT 'detection', atom_count INTEGER NOT NULL DEFAULT 0, "
        "updated_at REAL NOT NULL)"
    )
    c.execute("INSERT INTO encounters (id, start_time, updated_at) VALUES ('old', 1.0, 1.0)")
    db._apply_added_columns(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(encounters)")}
    assert {"tag", "place", "outcome", "tag_reason"} <= cols
    row = c.execute(f"SELECT {STAMP_COLS} FROM encounters WHERE id = 'old'").fetchone()
    assert tuple(row) == (None, None, None, None)  # existing rows stay unstamped
    c.close()


# ---- CLI: marcellus encounters restamp ------------------------------------


@pytest.fixture
def seeded_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "sidecar.db"
    cfg = tmp_path / "marcellus.yml"
    cfg.write_text(
        f"sidecar:\n  db_path: {db_path}\nencounters:\n  latitude: 47.6\n  longitude: -122.3\n"
    )
    monkeypatch.setenv("MARCELLUS_CONFIG", str(cfg))
    c = db.open_sidecar(db_path)
    try:
        noon = 1_782_165_600.0  # 2026-06-22 12:00 Pacific
        store.upsert_atom(c, _atom("p1", start=noon), LinkDecision("e_bg", "new", 1.0), noon)
        store.upsert_atom(
            c, _atom("c1", start=noon + 900, labels=("cat",)),
            LinkDecision("e_cat", "new", 1.0), noon,
        )
        store.upsert_atom(
            c, _atom("m1", start=noon + 2000), LinkDecision("e_multi", "new", 1.0), noon
        )
        store.upsert_atom(
            c, _atom("m2", camera="cam-b", start=noon + 2001),
            LinkDecision("e_multi", "adjacent", 0.9), noon,
        )
        c.commit()
    finally:
        c.close()
    return db_path


def _tags(db_path: Path) -> dict[str, tuple[Any, Any]]:
    c = db.open_sidecar(db_path)
    try:
        return {r["id"]: (r["tag"], r["tag_reason"]) for r in c.execute("SELECT * FROM encounters")}
    finally:
        c.close()


def test_restamp_dry_run_reports_without_writing(seeded_cli: Path) -> None:
    result = CliRunner().invoke(app, ["encounters", "restamp", "--dry-run"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["scanned"] == 3
    assert out["by_tag"] == {"background": 1, "notable": 2}
    assert out["by_reason"] == {"passer_by": 1, "animal": 1, "multi_camera": 1}
    assert set(_tags(seeded_cli).values()) == {(None, None)}


def test_restamp_writes_and_honours_since(seeded_cli: Path) -> None:
    noon = 1_782_165_600.0
    result = CliRunner().invoke(app, ["encounters", "restamp", "--since", str(noon + 1000)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["scanned"] == 1
    assert _tags(seeded_cli) == {
        "e_bg": (None, None),
        "e_cat": (None, None),
        "e_multi": ("notable", "multi_camera"),
    }
    result = CliRunner().invoke(app, ["encounters", "restamp"])
    assert json.loads(result.output)["scanned"] == 3
    assert _tags(seeded_cli) == {
        "e_bg": ("background", "passer_by"),
        "e_cat": ("notable", "animal"),
        "e_multi": ("notable", "multi_camera"),
    }


def test_restamp_batches_of_200(sidecar_db_path: Path) -> None:
    c = db.open_sidecar(sidecar_db_path)
    try:
        for i in range(450):
            store.upsert_atom(
                c, _atom(f"x{i}", start=NOON + i * 10_000.0),
                LinkDecision(f"e{i}", "new", 1.0), NOON,
            )
        c.commit()
        out = repair.restamp(c, _fake("background", "bulk"))
        assert out == {"scanned": 450, "by_tag": {"background": 450}, "by_reason": {"bulk": 450}}
        n = c.execute("SELECT COUNT(*) AS n FROM encounters WHERE tag = 'background'").fetchone()
        assert n["n"] == 450
    finally:
        c.close()


def test_service_reconcile_stamps_encounters(tmp_path: Path) -> None:
    import time as _time

    from marcellus.config import Settings
    from marcellus.encounters.adjacency import Adjacency
    from marcellus.encounters.service import EncounterService
    from tests.test_encounters_service import _insert_review, _reviewsegment_db

    frigate_db = _reviewsegment_db(tmp_path)
    now = _time.time()
    _insert_review(frigate_db, rid="r1", camera="alley-wide", start=now - 300, end=now - 290)
    _insert_review(frigate_db, rid="r2", camera="shed", start=now - 280, end=now - 270)
    _insert_review(frigate_db, rid="r3", camera="gate", start=now - 3000, end=now - 2995)
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate={"base_url": "http://frigate.test:5000", "config_path": cfg, "db_path": frigate_db},
        sidecar={"db_path": tmp_path / "sidecar.db"},
    )
    adjacency = Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})}))
    EncounterService(settings, adjacency=adjacency, now=lambda: now).reconcile()

    c = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = c.execute("SELECT cameras_json, tag, tag_reason FROM encounters").fetchall()
        by_cams = {tuple(json.loads(r["cameras_json"])): (r["tag"], r["tag_reason"]) for r in rows}
        assert by_cams[("alley-wide", "shed")] == ("notable", "multi_camera")
        assert by_cams[("gate",)][0] in ("background", "notable")
        assert all(r["tag"] is not None for r in rows)
    finally:
        c.close()

    # r2 vanishes from Frigate: the survivor is restamped as a single-camera visit.
    fc = sqlite3.connect(frigate_db)
    fc.execute("DELETE FROM reviewsegment WHERE id = 'r2'")
    fc.commit()
    fc.close()
    EncounterService(settings, adjacency=adjacency, now=lambda: now + 600).reconcile()
    c = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = c.execute(
            "SELECT tag_reason FROM encounters WHERE cameras_json = '[\"alley-wide\"]'"
        ).fetchone()
        assert row is not None and row["tag_reason"] != "multi_camera"
    finally:
        c.close()
