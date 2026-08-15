"""`GET`/`PUT /v1/push/settings` (Elsinore Phase 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, PushSection, Settings, SidecarSection
from frigate_sidecar.push import policy_settings
from frigate_sidecar.server import create_app


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _settings(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, **push_kwargs: Any,
) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(
        yaml.safe_dump(
            {
                "cameras": {
                    "doorbell": {
                        "zones": {
                            "front_door": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                    "street": {
                        "zones": {
                            "nw_49th_st": {"coordinates": "0,0,1,0,1,1,0,1"},
                            "sidewalk": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                    "backyard": {
                        "zones": {
                            "garage": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                }
            }
        )
    )
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp_path / "push_settings.json"), **push_kwargs,
        ),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    app = create_app(settings)
    return TestClient(app)


def test_get_returns_defaults_when_no_settings_file_exists(client: TestClient, tmp_path: Path):
    resp = client.get("/v1/push/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"] == policy_settings.default_settings()
    # And creates the file so GET and the routing engine never disagree.
    assert (tmp_path / "push_settings.json").exists()


def test_get_available_zones_from_frigate_config(client: TestClient):
    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["cameras"] == ["doorbell"]
    assert zones["front_door"]["guessed_class"] == "doors"
    assert zones["sidewalk"]["cameras"] == ["street"]
    assert zones["sidewalk"]["guessed_class"] == "street"
    assert zones["nw_49th_st"]["cameras"] == ["street"]
    assert zones["nw_49th_st"]["guessed_class"] == "street"  # via its camera, not its own name


def test_get_available_openings(client: TestClient):
    resp = client.get("/v1/push/settings")
    openings = set(resp.json()["available_openings"])
    assert "front_door" in openings
    assert "garage" in openings
    assert "sidewalk" not in openings


def test_put_persists_and_is_reflected_in_subsequent_get(client: TestClient, tmp_path: Path):
    new_settings = policy_settings.default_settings()
    new_settings["zone_classes"]["front_door"] = "doors"
    new_settings["routing_table"]["thing"]["yard"] = "urgent"
    new_settings["live_activities"]["package"] = False

    put_resp = client.put("/v1/push/settings", json=new_settings)
    assert put_resp.status_code == 200
    assert put_resp.json() == {"ok": True}

    get_resp = client.get("/v1/push/settings")
    stored = get_resp.json()["settings"]
    assert stored["zone_classes"] == {"front_door": "doors"}
    assert stored["routing_table"]["thing"]["yard"] == "urgent"
    assert stored["live_activities"]["package"] is False

    on_disk = json.loads((tmp_path / "push_settings.json").read_text())
    assert on_disk["routing_table"]["thing"]["yard"] == "urgent"


def test_put_persists_zone_overrides_and_get_returns_them(client: TestClient):
    new_settings = policy_settings.default_settings()
    new_settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}

    put_resp = client.put("/v1/push/settings", json=new_settings)
    assert put_resp.status_code == 200

    stored = client.get("/v1/push/settings").json()["settings"]
    assert stored["zone_overrides"] == {"front_entry_person": {"thing": "notify"}}


def test_put_rejects_invalid_zone_override(client: TestClient):
    bad = policy_settings.default_settings()
    bad["zone_overrides"] = {"driveway": {"thing": "screaming"}}
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400
    assert any("zone_overrides.driveway.thing" in d for d in resp.json()["detail"]["detail"])


def test_put_cleans_up_empty_zone_override_entries(client: TestClient):
    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"driveway": {}}
    resp = client.put("/v1/push/settings", json=settings)
    assert resp.status_code == 200

    stored = client.get("/v1/push/settings").json()["settings"]
    assert stored["zone_overrides"] == {}


def test_put_applies_immediately_to_the_routing_engine(client: TestClient):
    from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

    new_settings = policy_settings.default_settings()
    table_key = "routing_table_v2" if "routing_table_v2" in new_settings else "routing_table"
    new_settings[table_key]["thing"]["yard"] = "urgent"
    client.put("/v1/push/settings", json=new_settings)

    assert evaluate_ladder(Snapshot(subject="thing", place="yard")) == "urgent"


def test_put_rejects_invalid_level(client: TestClient):
    bad = policy_settings.default_settings()
    bad["routing_table"]["stranger"]["doors"] = "screaming"
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "invalid_settings"
    assert any("routing_table.stranger.doors" in d for d in body["detail"])


def test_put_rejects_unknown_subject(client: TestClient):
    bad = policy_settings.default_settings()
    bad["routing_table"]["ghost"] = {p: "log" for p in policy_settings.PLACES}
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400


def test_put_unknown_top_level_field_does_not_fail(client: TestClient):
    ok = policy_settings.default_settings()
    ok["a_future_field"] = {"whatever": True}
    resp = client.put("/v1/push/settings", json=ok)
    assert resp.status_code == 200


def test_get_includes_friendly_names_and_reloads_them(
    client: TestClient, tmp_path: Path,
):
    """`friendly_name` rides along in available_zones, and editing Frigate's
    config shows up on the next GET without a sidecar restart."""
    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["friendly_name"] is None

    config = tmp_path / "frigate-config.yml"
    doc = yaml.safe_load(config.read_text())
    doc["cameras"]["doorbell"]["zones"]["front_door"]["friendly_name"] = "Front Door"
    config.write_text(yaml.safe_dump(doc))

    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["friendly_name"] == "Front Door"


def test_get_includes_available_cameras(client: TestClient):
    resp = client.get("/v1/push/settings")
    assert resp.json()["available_cameras"] == ["backyard", "doorbell", "street"]


def test_put_round_trips_zone_page_fields(client: TestClient):
    """The /zones page PUTs the whole doc with classes, overrides, and an
    explicit camera_neighbors map."""
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["zone_classes"] = {"front_door": "doors", "garage": "off_limits"}
    doc["zone_overrides"] = {"sidewalk": {"person": "notify"}}
    doc["camera_neighbors"] = {"doorbell": ["street"]}

    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["zone_classes"] == {"front_door": "doors", "garage": "off_limits"}
    assert saved["zone_overrides"] == {"sidewalk": {"person": "notify"}}
    assert saved["camera_neighbors"] == {"doorbell": ["street"]}

    # An app-style PUT that omits camera_neighbors must not wipe it...
    app_doc = {k: v for k, v in doc.items() if k != "camera_neighbors"}
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_neighbors"] == {"doorbell": ["street"]}

    # ...but the page sending an explicit empty map clears it.
    doc["camera_neighbors"] = {}
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_neighbors"] == {}


def test_zones_page_renders(client: TestClient):
    resp = client.get("/zones")
    assert resp.status_code == 200
    assert "Camera neighbors" in resp.text
    assert "/static/js/zones.js" in resp.text


def test_put_round_trips_camera_calibration_fields(client: TestClient):
    """The /cameras page PUTs camera_headings (unit vectors, renormalized)
    and camera_layout (0..1 positions); app-style PUTs omitting them stay
    sticky."""
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_headings"] = {"doorbell": {"dx": 3.0, "dy": 4.0}}  # not unit length
    doc["camera_layout"] = {"doorbell": {"x": 0.25, "y": 0.75}}

    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_headings"] == {"doorbell": {"dx": 0.6, "dy": 0.8}}
    assert saved["camera_layout"] == {"doorbell": {"x": 0.25, "y": 0.75}}

    app_doc = {
        k: v for k, v in doc.items() if k not in ("camera_headings", "camera_layout")
    }
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_headings"] == {"doorbell": {"dx": 0.6, "dy": 0.8}}
    assert saved["camera_layout"] == {"doorbell": {"x": 0.25, "y": 0.75}}


def test_put_rejects_malformed_camera_calibration(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_headings"] = {"doorbell": {"dx": 0, "dy": 0}}  # zero vector
    assert client.put("/v1/push/settings", json=doc).status_code == 400
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_layout"] = {"doorbell": {"x": 1.5, "y": 0.5}}  # out of range
    assert client.put("/v1/push/settings", json=doc).status_code == 400


def test_cameras_page_renders(client: TestClient):
    resp = client.get("/cameras")
    assert resp.status_code == 200
    assert "/static/js/cameras.js" in resp.text


def test_camera_layout_accepts_azimuth_and_fov(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_layout"] = {
        "doorbell": {"x": 0.2, "y": 0.3, "azimuth": 365.0, "fov": 90},
        "street": {"x": 0.5, "y": 0.5},  # position-only stays valid
    }
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]["camera_layout"]
    assert saved["doorbell"] == {"x": 0.2, "y": 0.3, "azimuth": 5.0, "fov": 90.0}
    assert saved["street"] == {"x": 0.5, "y": 0.5}

    doc["camera_layout"] = {"doorbell": {"x": 0.2, "y": 0.3, "fov": 5}}  # fov too narrow
    assert client.put("/v1/push/settings", json=doc).status_code == 400
