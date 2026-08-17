"""New UI surfaces: status dashboard, debug index, devices, zone-hits, scrub viewer."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, Settings, SidecarSection
from frigate_sidecar.push import store
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",  # nothing listens: probes must degrade
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_status_page_is_home(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Status" in r.text
    # Frigate probe must degrade to "unreachable", not 500.
    assert "unreachable" in r.text


def test_status_json_shape(client: TestClient) -> None:
    r = client.get("/status.json")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    assert body["frigate"]["reachable"] is False
    assert body["scrub"]["enabled"] is False
    assert body["push"]["enabled"] is False
    assert body["push"]["device_count"] == 0
    assert "sizes" in body


def test_debug_page(client: TestClient) -> None:
    r = client.get("/debug")
    assert r.status_code == 200
    assert "/toybox" in r.text
    assert "Capabilities" in r.text


def test_toybox_not_in_main_nav(client: TestClient) -> None:
    r = client.get("/")
    assert 'class="page-link"' not in r.text or "Toybox" not in r.text


def test_zone_hits_page(client: TestClient) -> None:
    r = client.get("/zone-hits", params={"days": 7})
    assert r.status_code == 200


def test_devices_page_empty(client: TestClient) -> None:
    r = client.get("/devices")
    assert r.status_code == 200
    assert "no devices registered" in r.text


def test_devices_page_lists_registered(
    client: TestClient, sidecar_db_path: Path
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        store.upsert_device(
            conn,
            apns_token="ab" * 16,
            bundle_id="com.example.elsinore",
            environment="sandbox",
            app_version="1.0",
            cameras=["doorbell"],
            min_severity="alert",
        )
        conn.commit()
    finally:
        conn.close()
    r = client.get("/devices")
    assert r.status_code == 200
    assert "doorbell" in r.text
    assert "btn-primary" in r.text


def test_scrub_viewer_disabled(client: TestClient) -> None:
    r = client.get("/scrub")
    assert r.status_code == 200
    assert "disabled" in r.text


def test_triage_moved_to_slash_triage(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    assert "filters" in r.text


# ---- Frigate DB missing on this instance (dev host without the SQLite file) ----


@pytest.fixture
def db_less_client(tmp_path: Path, sidecar_db_path: Path) -> TestClient:
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",
            db_path=tmp_path / "nope" / "frigate.db",  # deliberately absent
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_triage_degrades_without_frigate_db(db_less_client: TestClient) -> None:
    r = db_less_client.get("/triage", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "database isn't available on this instance" in r.text
    # The shared nav still renders, so the page is a dead end, not a trap.
    assert 'class="page-link active"' in r.text


def test_analysis_api_degrades_without_frigate_db(db_less_client: TestClient) -> None:
    r = db_less_client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 503
    assert "Frigate DB not found" in r.json()["detail"]


def test_settings_page_unifies_surfaces(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    # One page, all the sections.
    for anchor in ("Zones &amp; routing", "Camera neighbors", "Push &amp; devices",
                   "Faces", "Data", "About"):
        assert anchor in r.text, anchor
    # Zones editor ids so zones.js drives the section unchanged.
    for element_id in ("zones-list", "neighbors-list", "save-btn",
                       "export-btn", "import-file"):
        assert element_id in r.text, element_id


def test_zones_and_devices_redirect_to_settings(client: TestClient) -> None:
    for path, fragment in (("/zones", "#zones"), ("/devices", "#push")):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/settings" + fragment
