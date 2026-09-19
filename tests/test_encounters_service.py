"""EncounterService: live hook, reconciler, PushEngine wiring
(docs/encounters.md "Tests" section)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection, Settings
from frigate_sidecar.encounters import store
from frigate_sidecar.encounters.adjacency import Adjacency
from frigate_sidecar.encounters.service import EncounterService
from frigate_sidecar.push.decision import parse_review_message
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import ReviewEvent
from frigate_sidecar.push.transport import LogTransport
from tests.test_scrub import REVIEWSEGMENT_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures"


def _settings(tmp_path: Path, frigate_db: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate={"base_url": "http://frigate.test:5000", "config_path": cfg, "db_path": frigate_db},
        sidecar={"db_path": tmp_path / "sidecar.db"},
    )


def _reviewsegment_db(tmp_path: Path, name: str = "frigate.db") -> Path:
    p = tmp_path / name
    conn = sqlite3.connect(p)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    conn.commit()
    conn.close()
    return p


def _insert_review(
    path: Path,
    *,
    rid: str,
    camera: str,
    start: float,
    end: float | None,
    severity: str = "alert",
    objects: tuple[str, ...] = ("person",),
    detections: tuple[str, ...] = (),
    zones: tuple[str, ...] = (),
    sub_labels: tuple[str, ...] = (),
) -> None:
    conn = sqlite3.connect(path)
    data = json.dumps(
        {
            "objects": list(objects),
            "detections": list(detections),
            "zones": list(zones),
            "sub_labels": list(sub_labels),
        }
    )
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rid, camera, start, end, severity, data),
    )
    conn.commit()
    conn.close()


def test_reconcile_over_reviewsegment(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 100,
        end=now - 90,
        objects=("person",),
        detections=("ev1",),
    )
    # NULL end_time: still-open review.
    _insert_review(
        frigate_db,
        rid="r2",
        camera="alley-wide",
        start=now - 80,
        end=None,
        objects=("person",),
        detections=("ev2",),
    )
    # A camera with a sidecar-side clock offset.
    _insert_review(
        frigate_db,
        rid="r3",
        camera="shed",
        start=now - 60,
        end=now - 50,
        objects=("raccoon",),
        detections=("ev3",),
    )

    settings = _settings(tmp_path, frigate_db)
    sidecar_conn = db.open_sidecar(settings.sidecar.db_path)
    db.set_event_clock_offset(sidecar_conn, "shed", 2000)
    sidecar_conn.commit()
    sidecar_conn.close()

    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)
    stats = service.reconcile()
    assert stats.rows == 3
    assert stats.new == 3

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        all_rows = conn.execute("SELECT * FROM encounter_members")
        rows = {r["atom_id"]: r for r in (dict(x) for x in all_rows)}
        assert set(rows) == {"r1", "r2", "r3"}
        # r2's end_time stays NULL (still open in Frigate).
        assert rows["r2"]["end_time"] is None
        # r3's start_time got the sidecar offset applied (2000ms = 2s).
        assert rows["r3"]["start_time"] == pytest.approx(now - 60 + 2.0)
        watermark = store.get_watermark(conn)
        assert watermark == pytest.approx(now - 60 + 2.0)  # max start_time seen, offset applied
    finally:
        conn.close()


def test_live_then_reconcile_agree_no_duplicates(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    # Live: a "new" then an "end" for the same review.
    ev_new = ReviewEvent(
        review_id="r1",
        camera="alley-wide",
        severity="alert",
        labels=("person",),
        msg_type="new",
        track_ids=("ev1",),
        start_time=now - 100,
    )
    service.observe_review(ev_new)
    ev_end = ReviewEvent(
        review_id="r1",
        camera="alley-wide",
        severity="alert",
        labels=("person",),
        msg_type="end",
        track_ids=("ev1",),
        start_time=now - 100,
    )
    service.observe_review(ev_end)

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute("SELECT * FROM encounter_members WHERE atom_id = 'r1'").fetchone()
        assert row is not None
        assert row["end_time"] == now  # set by the "end" hook

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()

    # Now the same review shows up in reviewsegment (as Frigate would persist
    # it) -- reconcile must not create a duplicate atom.
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 100,
        end=now - 95,
        objects=("person",),
        detections=("ev1",),
    )
    stats = service.reconcile()
    assert stats.rows == 1
    assert stats.new == 0
    assert stats.updated == 1

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
        row = conn.execute("SELECT end_time FROM encounter_members WHERE atom_id = 'r1'").fetchone()
        assert row["end_time"] == now - 95
    finally:
        conn.close()


def test_replay_capture_charger_loiter_produces_sane_grouping(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    fixture = FIXTURES / "capture-charger-loiter.jsonl"
    with fixture.open() as fh:
        for line in fh:
            msg = json.loads(line)
            if msg.get("topic") != "frigate/reviews":
                continue
            event = parse_review_message(msg["payload"])
            if event is not None:
                service.observe_review(event)

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n_members = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        n_encounters = conn.execute("SELECT COUNT(*) AS n FROM encounters").fetchone()["n"]
        assert n_members == 3  # 3 distinct review ids in the fixture (2 new + 1 update)
        assert 1 <= n_encounters <= 3
    finally:
        conn.close()


def test_observe_review_exception_does_not_propagate_through_handle_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    def _boom(ev: ReviewEvent) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "observe_review", _boom)

    engine = PushEngine(
        db_path=str(settings.sidecar.db_path),
        transport=LogTransport(),
        server_id="s1",
        on_review=service.observe_review,
    )
    engine.push_config = PushSection(delivery_enabled=True)

    import asyncio

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert", labels=("person",))
    # Must not raise, even though the hook always blows up.
    sent = asyncio.run(engine.handle_event(event))
    assert isinstance(sent, int)
