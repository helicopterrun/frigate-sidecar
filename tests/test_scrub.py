"""Tests for the `/v1` scrub-cache read layer (routes/scrub.py).

Covers docs/scrub-cache-and-proxy-spec.md §4.1 (capabilities), §4.4
(coverage -- latest_segment_end vs authoritative_through), and the §3.2
auth requirement.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, ScrubSection, Settings, SidecarSection
from frigate_sidecar.routes import scrub as scrub_routes
from frigate_sidecar.scrub import grid
from frigate_sidecar.server import create_app

RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id            VARCHAR(30) PRIMARY KEY,
    camera        VARCHAR(20) NOT NULL,
    path          VARCHAR(255) NOT NULL,
    start_time    DATETIME NOT NULL,
    end_time      DATETIME NOT NULL,
    duration      REAL NOT NULL,
    objects       INTEGER,
    motion        INTEGER,
    segment_size  REAL NOT NULL,
    dBFS          INTEGER,
    regions       INTEGER
);
"""

EVENT_SCHEMA = """
CREATE TABLE event (
    id           TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    start_time   REAL NOT NULL,
    end_time     REAL,
    score        REAL,
    top_score    REAL,
    zones        TEXT,
    data         TEXT
);
"""


def _skip_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(request: object) -> None:
        return None

    monkeypatch.setattr(scrub_routes, "_require_frigate_auth", _noop)


@pytest.fixture
def frigate_db_with_recordings(tmp_path: Path) -> Path:
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
    conn.executescript(EVENT_SCHEMA)
    now = time.time()
    # 10s segments, contiguous, ending 6.2s before "now" (matches measured
    # publish lag -- the last segment's end_time trails wall-clock).
    seg_end = now - 6.2
    for i in range(20):
        end = seg_end - i * 10
        start = end - 10
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, "
            "duration, segment_size) VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"seg{i}", f"/media/frigate/recordings/x/{i}.mp4", start, end),
        )
    # A gap: no segments between -400 and -300 relative to seg_end.
    # One closed event, one still-in-progress (end_time NULL -> "end": null).
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones) "
        "VALUES ('ev1', 'doorbell', 'person', ?, ?, 0.9, 0.92, '[\"front\"]')",
        (now - 200, now - 180),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones) "
        "VALUES ('ev2', 'doorbell', 'car', ?, NULL, 0.8, 0.85, '[]')",
        (now - 50,),
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def client(frigate_db_with_recordings: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_with_recordings,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    return TestClient(create_app(settings))


def test_capabilities_no_auth_required(client: TestClient) -> None:
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["scrub_cache"]["enabled"] is False
    assert body["scrub_cache"]["generated"] is False
    assert "http2" not in body  # dropped per review finding (§4.1)
    assert body["proxy"]["enabled"] is True


def test_coverage_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/coverage/doorbell", params={"start": 0, "end": time.time()})
    assert r.status_code == 401


def test_coverage_unknown_camera(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _skip_auth(request: object) -> None:
        return None

    monkeypatch.setattr(scrub_routes, "_require_frigate_auth", _skip_auth)
    r = client.get(
        "/v1/coverage/not-a-camera",
        params={"start": 0, "end": time.time()},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "camera_unknown"


def test_coverage_merges_intervals_and_splits_authoritative_from_latest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _skip_auth(request: object) -> None:
        return None

    monkeypatch.setattr(scrub_routes, "_require_frigate_auth", _skip_auth)

    now = time.time()
    r = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["recorded"], "contiguous segments should merge into recorded intervals"
    # latest_segment_end is the frozen diagnostic; authoritative_through tracks
    # wall-clock and is what the client should trust (docs spec §4.4 finding 4).
    assert body["latest_segment_end"] < body["authoritative_through"]
    assert abs(body["authoritative_through"] - (now - db.DEFAULT_PUBLISH_LAG_S)) < 1.0
    assert body["retention_days"] == 4
    assert body["queried"] == [now - 300, now]


def test_authoritative_through_survives_camera_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera with no new segments for a while: latest_segment_end stays
    frozen, but authoritative_through keeps advancing at wall-clock rate --
    their divergence is the outage signal (docs spec §4.4 finding 4)."""

    async def _skip_auth(request: object) -> None:
        return None

    monkeypatch.setattr(scrub_routes, "_require_frigate_auth", _skip_auth)

    now = time.time()
    r1 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    later = now + 120  # camera has been silent for 2 more minutes
    # Simulate by directly calling the DB helper with a later `now`, since the
    # fixture data doesn't change -- latest_segment_end must not move while
    # authoritative_through does.
    conn = db.open_frigate_ro(client.app.state.settings.frigate.db_path)  # type: ignore[attr-defined]
    try:
        r2 = db.recording_coverage(conn, "doorbell", now - 300, now, now=later)
    finally:
        conn.close()

    r1_body = r1.json()
    assert r2["latest_segment_end"] == pytest.approx(r1_body["latest_segment_end"])
    assert r2["authoritative_through"] > r1_body["authoritative_through"]


def _seed_bucket(
    sidecar_db_path: Path, camera: str, start: float, end: float, interval: float
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        db.upsert_scrub_bucket(
            conn, camera=camera, start_ts=start, end_ts=end, interval_s=interval,
            width=320, height=180, generated_through=end, complete=True,
        )
        conn.commit()
    finally:
        conn.close()


def test_scrub_coverage_interval_contract_and_retention_distinction(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    # Recent 1fps bucket.
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 1.0)
    # Older, coarser (aged) bucket.
    _seed_bucket(sidecar_db_path, "doorbell", now - 3600, now - 3000, 5.0)

    r = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": now - 4000, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    intervals = {b["interval"] for b in body["buckets"]}
    assert intervals == {1.0, 5.0}
    assert body["generated_through"] == pytest.approx(now - 40)
    assert body["retention_days"] == 4

    # A span entirely past retention_days has no buckets at all -- distinct
    # from "lagging" only because the client compares queried range against
    # retention_days (both present in the response), per spec §4.2.
    ancient_start = now - 20 * 86400
    r2 = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": ancient_start, "end": ancient_start + 100},
        headers={"cookie": "session=fake"},
    )
    body2 = r2.json()
    assert body2["buckets"] == []
    queried_age_days = (now - ancient_start) / 86400
    assert queried_age_days > body2["retention_days"]  # client's "will never exist" check


def test_scrub_sheet_url_immutable_across_growing_count(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    start = 1_785_380_400.0

    def _write_sheet(count: int) -> str:
        rel = grid.sheet_rel_path("doorbell", 1.0, start, count)
        out = cache_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10)).save(out)
        return rel

    conn = db.open_sidecar(sidecar_db_path)
    try:
        for count in (12, 40):
            rel = _write_sheet(count)
            db.upsert_scrub_sheet(
                conn, camera="doorbell", start_ts=start, interval_s=1.0, cols=12, rows=8,
                cell_w=320, cell_h=180, count=count, path=rel, complete=(count == 96),
            )
        conn.commit()
    finally:
        conn.close()

    r_index = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    sheets = r_index.json()["sheets"]
    assert len(sheets) == 1
    assert sheets[0]["count"] == 40  # only the latest version is advertised
    assert sheets[0]["url"] == "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg"

    r12 = client.get(
        "/v1/scrub/doorbell/sheet/1785380400-1.0-12.jpg", headers={"cookie": "session=fake"}
    )
    r40 = client.get(
        "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg", headers={"cookie": "session=fake"}
    )
    assert r12.status_code == 200
    assert r40.status_code == 200
    # Two different generations of the same live sheet -> two different URLs
    # (differing count); both served unconditionally immutable (spec §4.3,
    # §11 test_scrub_sheet_url_immutable) -- no cache-freshness logic anywhere.
    assert r12.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r40.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_reel_bundle_shape(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> list[float]:
        n = int((end - start) / scale)
        return [0.0] * n

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    _seed_bucket(sidecar_db_path, "doorbell", now - 300, now - 200, 1.0)
    _seed_bucket(sidecar_db_path, "doorbell", now - 3600, now - 3000, 5.0)

    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("queried", "recorded", "latest_segment_end", "authoritative_through",
                "frames", "motion", "events"):
        assert key in body

    assert body["queried"] == [now - 3700, now]
    assert isinstance(body["frames"], list)
    assert len(body["frames"]) == 2  # straddles the 1.0/5.0 thinning boundary
    assert isinstance(body["motion"]["values"], list)
    assert len(body["motion"]["values"]) == int(3700 / 10)  # zero-filled to full range

    events_by_id = {e["id"]: e for e in body["events"]}
    assert events_by_id["ev1"]["end"] is not None
    assert events_by_id["ev2"]["end"] is None  # still-in-progress -> null, not omitted/placeholder


def test_motion_totalizes_full_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> list[float]:
        from frigate_sidecar.scrub.motion import aggregate_motion

        # Only 9 of the requested buckets have "upstream" data -- simulates
        # the measured short-window cliff (§4.6).
        points = [(start + i * scale, 55.0) for i in range(9)]
        return aggregate_motion(points, start, end, scale)

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    r = client.get(
        "/v1/motion/doorbell",
        params={"start": now - 1800, "end": now, "scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["values"]) == 30  # full 1800/60, not truncated
    assert body["values"][:9] == [55.0] * 9
    assert body["values"][9:] == [0.0] * 21


def test_highlights_use_frigate_label_vocabulary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    reasons = {h["reason"] for h in body["highlights"]}
    assert reasons <= {"person", "car"}  # Frigate object labels, not a separate scheme
    for h in body["highlights"]:
        assert "score" in h


def test_unknown_v1_path_is_json_404_not_html(client: TestClient) -> None:
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_coverage_etag_304(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    # Freeze time for this endpoint so authoritative_through (which otherwise
    # advances every call) doesn't change the body -- and with it the ETag --
    # between the two requests below.
    monkeypatch.setattr(scrub_routes.time, "time", lambda: now)
    r1 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    etag = r1.headers["etag"]
    r2 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake", "if-none-match": etag},
    )
    assert r2.status_code == 304
