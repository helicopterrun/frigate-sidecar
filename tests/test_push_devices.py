from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, Settings, SidecarSection
from frigate_sidecar.push import store
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_register_device(client: TestClient) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={
            "bundle_id": "com.pondhouse.Elsinore",
            "environment": "sandbox",
            "app_version": "0.3.0",
            "cameras": ["doorbell", "garden"],
            "labels": ["person", "car"],
            "min_severity": "alert",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["device_id"].startswith("d_")


def test_register_is_idempotent_on_token(client: TestClient) -> None:
    r1 = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["doorbell"]},
    )
    r2 = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["garden"]},
    )
    assert r1.json()["device_id"] == r2.json()["device_id"]


def test_register_overwrites_filters_not_duplicates(
    client: TestClient, sidecar_db_path: Path
) -> None:
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["doorbell"]},
    )
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["garden"]},
    )
    from frigate_sidecar import db

    conn = db.open_sidecar(sidecar_db_path)
    try:
        rows = conn.execute("SELECT * FROM push_devices").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    device = store.list_devices(db.open_sidecar(sidecar_db_path))[0]
    assert device.cameras == ("garden",)


def test_environment_required_and_validated(client: TestClient) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "production"},
    )
    assert r.status_code == 422


def test_unregister_device(client: TestClient) -> None:
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox"},
    )
    r = client.delete("/v1/push/devices/tok-abc123")
    assert r.status_code == 200
    assert r.json() == {"unregistered": True}


def test_unregister_unknown_token_is_still_200(client: TestClient) -> None:
    r = client.delete("/v1/push/devices/never-registered")
    assert r.status_code == 200
    assert r.json() == {"unregistered": True}


def test_capabilities_reports_push_disabled_by_default(client: TestClient) -> None:
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    assert r.json()["push"] == {"enabled": False, "transport": "mock"}
