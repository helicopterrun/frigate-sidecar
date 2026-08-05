"""The v2 `/v1/push` surface (notification-experience plan §8, handoff 1-6).

Registration keeps every v1 guarantee; the four new endpoints are the app's
whole Phase-1 vocabulary.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, PushSection, Settings, SidecarSection
from frigate_sidecar.push import store
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.transport import LogTransport
from frigate_sidecar.server import create_app

TOKEN = "tok-abc123"

AT_THE_DOOR: dict[str, Any] = {
    "id": "at-the-door", "name": "At the door", "tier": "interrupt",
    "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
    "loiter_seconds": 5, "sound": "chime",
}


def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        push=PushSection(enabled=False),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    app = create_app(settings)
    app.state.push_engine = PushEngine(
        db_path=str(sidecar_db_path), transport=LogTransport(), server_id="s_test",
        frigate_base_url="",
    )
    return TestClient(app)


def _register(client: TestClient, **overrides: Any) -> Any:
    body: dict[str, Any] = {"bundle_id": "com.x", "environment": "sandbox"}
    body.update(overrides)
    return client.put(f"/v1/push/devices/{TOKEN}", json=body)


# -- registration ------------------------------------------------------------


def test_v1_registration_still_reports_schema_version_1(client: TestClient) -> None:
    r = _register(client, cameras=["doorbell"])
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["schema_version"] == 1
    assert body["situations_accepted"] == 0


def test_v2_registration_accepts_the_full_section_8_record(client: TestClient) -> None:
    r = _register(
        client,
        schema_version=2,
        timezone="America/Los_Angeles",
        location={"lat": 45.51, "lon": -122.68},
        situations=[AT_THE_DOOR],
        morning_digest={"enabled": True, "hour": 7, "minute": 0},
        llm={"mode": "off"},
        live_activity_token="la-tok",
        snoozes=[{"scope": "situation:at-the-door", "until": time.time() + 900}],
    )
    assert r.status_code == 200
    assert r.json()["schema_version"] == 2
    assert r.json()["situations_accepted"] == 1


def test_registration_reports_situations_it_could_not_parse(client: TestClient) -> None:
    """A rule the sidecar silently discarded would look enabled in the app
    and never fire."""
    r = _register(client, situations=[AT_THE_DOOR, {"name": "no id here"}])
    assert r.json()["situations_accepted"] == 1


def test_unknown_fields_do_not_422(client: TestClient) -> None:
    """A newer app build must not fail against an older sidecar."""
    r = _register(client, situations=[AT_THE_DOOR], some_future_field={"a": 1})
    assert r.status_code == 200


def test_reregistering_without_snoozes_leaves_them_alone(client: TestClient) -> None:
    """The app re-registers on every launch; a launch must not cancel a
    snooze the user set an hour ago (plan's "survives app kill")."""
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    _register(client, situations=[AT_THE_DOOR])  # no `snoozes` key

    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    assert [s["scope"] for s in r.json()["active"]] == ["global"]


def test_explicit_empty_snoozes_clears_them(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    _register(client, situations=[AT_THE_DOOR], snoozes=[])
    r = client.delete(f"/v1/push/snooze/global?apns_token={TOKEN}")
    assert r.json()["active"] == []


# -- starter library + sounds ------------------------------------------------


def test_starter_library_has_the_four_named_starters(client: TestClient) -> None:
    r = client.get("/v1/push/situations/library")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()]
    assert ids == [
        "at-the-door", "near-my-car", "package-delivery", "unknown-vehicle-parked"
    ]


def test_starter_library_round_trips_into_a_registration(client: TestClient) -> None:
    """The library's output must be directly usable as registration input --
    the app enables a starter with one tap and edits its zones."""
    starters = client.get("/v1/push/situations/library").json()
    r = _register(client, situations=starters)
    assert r.json()["situations_accepted"] == len(starters)


def test_starter_sounds_all_exist_in_the_catalog(client: TestClient) -> None:
    catalog = {s["id"] for s in client.get("/v1/push/sounds").json()}
    for starter in client.get("/v1/push/situations/library").json():
        assert starter["sound"] in catalog


def test_sound_catalog_shape(client: TestClient) -> None:
    r = client.get("/v1/push/sounds")
    assert r.status_code == 200
    assert all(set(s) == {"id", "name"} for s in r.json())
    assert client.get("/v1/push/sounds?app_version=1.0").status_code == 200


# -- snooze ------------------------------------------------------------------


def test_snooze_and_unsnooze_round_trip(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    until = time.time() + 900
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "situation:at-the-door", "until_epoch": until},
    )
    assert r.status_code == 200 and r.json()["snoozed"] is True

    r = client.delete(f"/v1/push/snooze/situation:at-the-door?apns_token={TOKEN}")
    assert r.status_code == 200
    assert r.json()["active"] == []


def test_snooze_for_unknown_device_is_404(client: TestClient) -> None:
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": "never-registered", "scope": "global", "until_epoch": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "device_not_found"


@pytest.mark.parametrize("scope", ["", "situation:", "camera:", "everything"])
def test_malformed_scope_is_rejected(client: TestClient, scope: str) -> None:
    """A scope that silences nothing while looking like it took is worse than
    an error."""
    _register(client, situations=[AT_THE_DOOR])
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": scope, "until_epoch": time.time() + 60},
    )
    assert r.status_code == 422


def test_camera_scope_survives_a_camera_name_with_punctuation(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "camera:back-yard", "until_epoch": time.time() + 60},
    )
    r = client.delete(f"/v1/push/snooze/camera:back-yard?apns_token={TOKEN}")
    assert r.json()["active"] == []


def test_unsnoozing_something_never_snoozed_is_still_200(client: TestClient) -> None:
    r = client.delete(f"/v1/push/snooze/global?apns_token={TOKEN}")
    assert r.status_code == 200


# -- per-situation test push -------------------------------------------------


def test_situation_test_push_fires_the_real_shape(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    r = client.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"sent": True, "situation_id": "at-the-door"}

    transport = client.app.state.push_engine.transport
    payload = transport.sent[-1]["payload"]
    assert payload["situation_id"] == "at-the-door"
    assert payload["aps"]["category"] == "situation.at-the-door"
    assert payload["handle"].startswith("h_")


def test_situation_test_does_not_spend_the_hourly_budget(
    client: TestClient, sidecar_db_path: Path
) -> None:
    _register(client, situations=[AT_THE_DOOR])
    for _ in range(3):
        client.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    conn = db.open_sidecar(sidecar_db_path)
    assert store.count_sends_since(
        conn, apns_token=TOKEN, situation_id="at-the-door", since=0
    ) == 0
    conn.close()


def test_situation_test_for_unregistered_situation_is_404(client: TestClient) -> None:
    """Falling back to the starter library would test a rule the device isn't
    registered with."""
    _register(client, situations=[AT_THE_DOOR])
    r = client.post("/v1/push/test/near-my-car", json={"apns_token": TOKEN})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "situation_not_found"


def test_situation_test_for_unknown_device_is_404(client: TestClient) -> None:
    r = client.post("/v1/push/test/at-the-door", json={"apns_token": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "device_not_found"


def test_situation_test_with_push_disabled_is_503(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """503, never 404: the released client reads a 404 on the test path as
    "this server is too old", which would be the wrong story entirely."""
    app = create_app(_settings(frigate_db_path, sidecar_db_path, tmp_path))
    plain = TestClient(app)  # no push_engine on app.state
    plain.put(
        f"/v1/push/devices/{TOKEN}",
        json={"bundle_id": "com.x", "environment": "sandbox", "situations": [AT_THE_DOOR]},
    )
    r = plain.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "push_disabled"


# -- thumbnail ---------------------------------------------------------------


def test_thumbnail_round_trip(client: TestClient, sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="ev1", review_id="r1", ttl_s=3600
    )
    store.store_thumbnail(conn, handle, b"\xff\xd8\xff-not-really-a-jpeg")
    conn.commit()
    conn.close()

    r = client.get(f"/v1/push/thumbnail/{handle}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8\xff-not-really-a-jpeg"


def test_thumbnail_miss_is_404_not_a_hang(client: TestClient) -> None:
    """The NSE delivers the alert without an image on a miss -- the visible
    push is the promise, the image is not."""
    r = client.get("/v1/push/thumbnail/h_nope")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "thumbnail_not_found"


def test_expired_handle_yields_no_thumbnail(
    client: TestClient, sidecar_db_path: Path
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="ev1", review_id="r1", ttl_s=-1
    )
    store.store_thumbnail(conn, handle, b"jpeg")
    conn.commit()
    conn.close()
    assert client.get(f"/v1/push/thumbnail/{handle}").status_code == 404


def test_thumbnail_requires_auth_like_every_other_v1_route(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    settings.sidecar.require_frigate_auth = True
    authed = TestClient(create_app(settings))
    assert authed.get("/v1/push/thumbnail/h_x").status_code in (401, 403)
