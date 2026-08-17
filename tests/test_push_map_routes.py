"""`GET /v1/push/map/zones` and `/map/live` — floorplan overlay data."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, PushSection, Settings, SidecarSection
from frigate_sidecar.push import policy_settings
from frigate_sidecar.server import create_app

OPTICS = {"hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0}
LAYOUT = {"x": 0.5, "y": 0.5, "azimuth": 0.0, "fov": 90.0}


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _make_client(tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    config = tmp_path / "frigate-config.yml"
    config.write_text(yaml.safe_dump({
        "cameras": {
            # A floor-hugging zone (projectable) on a placed camera.
            "cam": {"zones": {"walkway": {"coordinates": "0.3,0.6,0.7,0.6,0.7,0.9,0.3,0.9"}}},
            # Full-frame gate zone: must be skipped.
            "gate": {"zones": {"all": {"coordinates": "0,0,1,0,1,1,0,1"}}},
            # Camera with a zone but no layout: omitted.
            "unplaced": {"zones": {"side": {"coordinates": "0.3,0.6,0.7,0.6,0.5,0.9"}}},
        }
    }))
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp_path / "push_settings.json"),
        ),
    )
    app = create_app(settings)
    return TestClient(app)


def _apply_map_policy(**extra):
    active = dict(policy_settings.get_active())
    active.update({
        "camera_optics": {"cam": dict(OPTICS), "gate": dict(OPTICS)},
        "camera_layout": {"cam": dict(LAYOUT), "gate": dict(LAYOUT)},
        "map_scale_ft": 200.0,
    })
    active.update(extra)
    policy_settings.apply_settings(active)


def test_map_zones_projects_placed_cameras_only(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/zones").json()
    assert body["aspect"] == 1.0
    names = [(z["camera"], z["name"]) for z in body["zones"]]
    assert names == [("cam", "walkway")]
    z = body["zones"][0]
    assert len(z["points"]) >= 3
    assert all(len(p) == 2 for p in z["points"])


def test_map_zones_without_scale_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    body = client.get("/v1/push/map/zones").json()
    assert body["zones"] == []


def test_map_live_without_engine_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/live").json()
    assert body["objects"] == []
    assert body["t"] > 0


def test_map_live_serves_fused_tracks(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    import time

    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()

    from frigate_sidecar.push.situations import TrackStore

    class _Engine:
        tracks = TrackStore()

    engine = _Engine()
    now = time.time()
    engine.tracks.observe_object(
        "cam", "t1", (), now=now, path_data=((0.5, 0.7, now),), label="person",
    )
    client.app.state.push_engine = engine
    body = client.get("/v1/push/map/live?debug=1").json()
    assert len(body["objects"]) == 1
    obj = body["objects"][0]
    assert obj["label"] == "person"
    assert obj["cameras"] == ["cam"]
    assert obj["members"][0]["forward_ft"] > 0


def test_map_footprints_projects_placed_cameras(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/footprints").json()
    cams = [f["camera"] for f in body["footprints"]]
    assert cams == ["cam", "gate"]  # placed+optics only; sorted
    fp = body["footprints"][0]
    assert len(fp["points"]) >= 3
    assert fp["clipped"] is True  # frame top is sky at 12 deg tilt


def test_map_footprints_without_scale_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    assert client.get("/v1/push/map/footprints").json()["footprints"] == []
