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

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, ScrubSection, Settings, SidecarSection
from frigate_sidecar.routes import scrub as scrub_routes
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


@pytest.fixture
def frigate_db_with_recordings(tmp_path: Path) -> Path:
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
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
        scrub=ScrubSection(enabled=False, retention_days=4),
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
