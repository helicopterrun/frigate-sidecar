"""`location` as a general, live-editable setting: stored as tuning overrides
(`config/tuning.json`), read at call time by the encounter stamp, shown with a
sun check on /settings."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from marcellus import tuning
from marcellus.config import FrigateSection, Settings, SidecarSection, load_settings
from marcellus.encounters import notability
from marcellus.push import policy_settings
from marcellus.server import create_app

SEATTLE = {"location.latitude": 47.6, "location.longitude": -122.3}
_PACIFIC = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _isolated():
    tuning.reset_for_tests()
    policy_settings.reset_for_tests()
    yield
    tuning.reset_for_tests()
    policy_settings.reset_for_tests()


@pytest.fixture
def settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras:\n  porch:\n    zones: {}\n")
    s = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path,
            bind_port=5001,
            require_frigate_auth=False,
            tuning_path=str(tmp_path / "tuning.json"),
        ),
    )
    tuning.snapshot_startup(s)
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _put(client: TestClient, overrides: dict[str, Any]) -> Any:
    rev = client.get("/v1/tuning").json()["rev"]
    return client.put("/v1/tuning", json={"rev": rev, "overrides": overrides})


def test_location_knobs_are_live_and_editable() -> None:
    for key in ("location.latitude", "location.longitude", "location.label"):
        knob = tuning.KNOBS_BY_KEY[key]
        assert knob.editable and knob.live, key
    assert tuning.KNOBS_BY_KEY["location.latitude"].max == 90
    assert tuning.KNOBS_BY_KEY["location.longitude"].min == -180


def test_default_is_unset(client: TestClient) -> None:
    body = client.get("/v1/tuning").json()
    assert body["location"] == {"sun_up": None}
    rows = {k["key"]: k for k in body["knobs"]}
    assert rows["location.latitude"]["value"] is None


def test_put_applies_live_persists_and_reports_the_sun(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    resp = _put(client, {**SEATTLE, "location.label": "Home, Seattle"})
    assert resp.status_code == 200, resp.text
    assert (settings.location.latitude, settings.location.longitude) == (47.6, -122.3)
    assert settings.location.label == "Home, Seattle"
    assert isinstance(resp.json()["location"]["sun_up"], bool)
    saved = json.loads((tmp_path / "tuning.json").read_text())
    assert saved["location.latitude"] == 47.6 and saved["location.label"] == "Home, Seattle"
    again = client.get("/v1/tuning").json()["location"]["sun_up"]
    assert again is resp.json()["location"]["sun_up"]


def test_sun_up_matches_is_night(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    noon = datetime(2026, 6, 22, 12, 0, tzinfo=_PACIFIC).timestamp()
    midnight = datetime(2026, 6, 22, 0, 0, tzinfo=_PACIFIC).timestamp()
    monkeypatch.setattr("marcellus.routes.tuning.time.time", lambda: noon)
    assert _put(client, SEATTLE).json()["location"]["sun_up"] is True
    monkeypatch.setattr("marcellus.routes.tuning.time.time", lambda: midnight)
    assert client.get("/v1/tuning").json()["location"]["sun_up"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"location.latitude": 47.6},  # both or neither
        {"location.longitude": -122.3},
        {"location.latitude": 91.0, "location.longitude": 0.0},
        {"location.latitude": 0.0, "location.longitude": 181.0},
        {"location.latitude": "47.6", "location.longitude": -122.3},
    ],
)
def test_put_rejects_bad_location(client: TestClient, overrides: dict[str, Any]) -> None:
    assert _put(client, overrides).status_code == 400


def test_removing_the_override_clears_the_location(
    client: TestClient, settings: Settings
) -> None:
    _put(client, SEATTLE)
    resp = _put(client, {})
    assert resp.status_code == 200
    assert settings.location.latitude is None and settings.location.longitude is None
    assert resp.json()["location"] == {"sun_up": None}


def test_stamper_for_reads_the_location_at_call_time(
    client: TestClient, settings: Settings
) -> None:
    """The stamper built at startup sees a location saved afterwards."""
    stamper = notability.stamper_for(settings)
    start = datetime(2026, 6, 22, 12, 0, tzinfo=_PACIFIC).timestamp()  # noon in Seattle
    member = {
        "camera": "cam-a", "start_time": start, "end_time": start + 5,
        "labels": ["person"], "zones": ["sidewalk"], "sub_labels": [],
    }
    assert stamper([member]).reason == "passer_by"
    # Longitude on the far side of the planet puts the same instant at night.
    resp = _put(client, {"location.latitude": 47.6, "location.longitude": 57.7})
    assert resp.status_code == 200
    assert stamper([member]).reason == "night"


def test_load_settings_applies_location_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tuning.json").write_text(
        json.dumps({"_rev": 1, **SEATTLE, "location.label": "home"})
    )
    s = load_settings()
    assert (s.location.latitude, s.location.longitude, s.location.label) == (47.6, -122.3, "home")


def test_load_settings_drops_a_lone_coordinate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tuning.json").write_text(
        json.dumps({"_rev": 1, "location.latitude": 47.6, "scrub.retention_days": 9})
    )
    with caplog.at_level(logging.WARNING, logger="marcellus.config"):
        s = load_settings()
    assert s.location.latitude is None
    assert s.scrub.retention_days == 9  # the rest of the file still applies
    assert "location.latitude" in caplog.text


def test_settings_page_has_the_location_block_and_camera_places(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'id="location"' in r.text
    assert 'href="#location"' in r.text
    assert 'id="tuning-grid-location"' in r.text
    assert 'id="camera-places"' in r.text
    m = re.search(r"data-places='([^']*)'", r.text)
    assert m is not None
    # A camera with no zones is listed, with its name-based guess.
    assert json.loads(m.group(1)) == {"cameras": ["porch"], "guesses": {"porch": "yard"}}
    assert "Used when something is seen outside every zone." in r.text


def test_the_page_sets_no_content_security_policy(client: TestClient) -> None:
    """The in-browser address lookup (fetch to nominatim.openstreetmap.org)
    relies on there being no connect-src restriction."""
    r = client.get("/settings")
    assert "content-security-policy" not in {k.lower() for k in r.headers}
    assert "Content-Security-Policy" not in r.text
