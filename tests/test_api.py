from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.config import (
    FrigateSection,
    Settings,
    SidecarSection,
)
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    # Frigate config.yml is optional; sidecar should handle a missing file.
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
    )
    return TestClient(create_app(settings))


def test_list_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "frigate-sidecar" in r.text
    # Each fixture camera should appear in the camera filter.
    assert "alley-overview" in r.text
    assert "street-overview" in r.text


def test_list_filter_by_camera(client: TestClient) -> None:
    r = client.get("/", params={"camera": "alley-east", "days": 7})
    assert r.status_code == 200
    # `alley-east` is in the fixture but only as a dog event; should render.
    assert "alley-east" in r.text


def test_detail_html(client: TestClient) -> None:
    r = client.get("/event/e1")
    assert r.status_code == 200
    assert "alley-overview" in r.text
    # Detail page exposes label buttons.
    assert 'data-label="tp"' in r.text


def test_detail_404(client: TestClient) -> None:
    r = client.get("/event/does-not-exist")
    assert r.status_code == 404


def test_label_round_trip(client: TestClient) -> None:
    r = client.post(
        "/label",
        json={"event_id": "e1", "label": "fp", "note": "porch shadow"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # The list view filtered to fp should now include e1.
    r2 = client.get("/", params={"triage": "fp"})
    assert "e1" in r2.text


def test_label_invalid(client: TestClient) -> None:
    r = client.post("/label", json={"event_id": "e1", "label": "garbage"})
    assert r.status_code == 400


def test_clear_label(client: TestClient) -> None:
    client.post("/label", json={"event_id": "e1", "label": "fp"})
    r = client.post("/clear-label", json={"event_id": "e1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_motion_blank_form(client: TestClient) -> None:
    # No baseline/target: page renders the form + a "set a target" empty state.
    r = client.get("/motion")
    assert r.status_code == 200
    assert "frigate-sidecar" in r.text
    # Header nav renders both pages and motion is the active link.
    assert 'class="page-link active"' in r.text
    assert "Motion" in r.text
    assert "Triage" in r.text


def test_motion_error_on_unreachable_frigate(client: TestClient) -> None:
    # Live API not reachable -> error banner, but page still 200.
    r = client.get("/motion", params={"baseline": "yesterday", "target": "today"})
    assert r.status_code == 200
    assert "Frigate API unreachable" in r.text or "date parse error" in r.text


def test_list_has_active_nav(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    # Verify the nav with active=triage shows up.
    assert "page-link active" in r.text


# ----- Analysis HTTP routes -----


def test_analysis_motion_rate(client: TestClient) -> None:
    r = client.get("/analysis/motion-rate", params={"days": 7})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row["camera"] == "alley-overview" for row in rows)


def test_analysis_score_histogram(client: TestClient) -> None:
    r = client.get("/analysis/score-histogram", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "buckets" in body


def test_analysis_zone_hits(client: TestClient) -> None:
    r = client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "hits" in body and "mask_candidates" in body


def test_analysis_pull_events(client: TestClient) -> None:
    r = client.get("/analysis/pull-events", params={"days": 7, "limit": 100})
    assert r.status_code == 200
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert ids == {"e1", "e2", "e3", "e4"}


def test_analysis_pull_events_camera_filter(client: TestClient) -> None:
    r = client.get(
        "/analysis/pull-events", params={"days": 7, "camera": "alley-overview"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert {row["camera"] for row in rows} == {"alley-overview"}


def test_analysis_fps_budget_502_on_api_failure(client: TestClient) -> None:
    # No real Frigate API at the configured base URL in this test -> 502.
    r = client.get("/analysis/fps-budget")
    assert r.status_code == 502
