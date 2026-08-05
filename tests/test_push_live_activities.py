"""Live Activities: the stage machine, the three push shapes, escalation.

Phase 2's "done looks like" list, one test per line -- plus the two rules it
is easiest to violate: a device without a push-to-start token keeps working,
and an escalation is *one* alert carrying the activity's own collapse id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, PushSection, Settings, SidecarSection
from frigate_sidecar.push import store
from frigate_sidecar.push.activity import ATTRIBUTES_TYPE
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import ReviewEvent
from frigate_sidecar.push.situations import Escalation, Match, Situation
from frigate_sidecar.push.transport import LogTransport, TransportResult
from frigate_sidecar.server import create_app

NOW = 1_785_000_000.0
PTS = "push-to-start-token"

PACKAGE_DELIVERY: dict[str, Any] = {
    "id": "package-delivery", "name": "Package delivery", "tier": "present",
    "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
    "loiter_seconds": 3, "sound": "marimba",
    "escalation": {"from_tier": "present", "to_tier": "interrupt",
                   "on": "loiter_exceeds:5"},
}
AT_THE_DOOR: dict[str, Any] = {
    "id": "at-the-door", "name": "At the door", "tier": "interrupt",
    "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
    "loiter_seconds": 5,
}


def _engine(db_path: Path, transport=None, **kw) -> PushEngine:
    return PushEngine(
        db_path=str(db_path), transport=transport or LogTransport(), server_id="s1",
        frigate_base_url="", **kw,
    )


def _register(db_path: Path, token: str, **kwargs: Any) -> None:
    conn = db.open_sidecar(db_path)
    store.upsert_device(
        conn, apns_token=token, bundle_id="com.x", environment="sandbox", **kwargs
    )
    conn.commit()
    conn.close()


def _review(**kw: Any) -> ReviewEvent:
    base: dict[str, Any] = dict(
        review_id="r1", camera="doorbell", severity="alert", labels=("person",),
        zones=("porch",), track_ids=("t1",), start_time=NOW,
    )
    base.update(kw)
    return ReviewEvent(**base)


def _object(track_id: str = "t1", zones=("porch",), type_="update", **extra):
    after = {"id": track_id, "camera": "doorbell", "label": "person",
             "current_zones": list(zones), "entered_zones": list(zones)}
    after.update(extra)
    return {"type": type_, "after": after}


def _run(coro):
    return asyncio.run(coro)


def _backdate(engine: PushEngine, seconds: float, zone: str = "porch") -> None:
    """Advance the clock as the track sees it: dwell grows by `seconds`."""
    for state in engine.tracks._tracks.values():  # noqa: SLF001
        if zone in state.first_seen_in_zone:
            state.first_seen_in_zone[zone] -= seconds


def _age_pushes(db_path: Path, seconds: float) -> None:
    """Advance the clock as the *activity* sees it.

    Separate from `_backdate` because they are separate clocks: dwell decides
    what to say, `last_push_at` decides whether we're allowed to say it yet.
    A test that only moves the first one is testing the coalescing window, not
    whatever it meant to test.
    """
    conn = db.open_sidecar(db_path)
    conn.execute("UPDATE push_activities SET last_push_at = last_push_at - ?", (seconds,))
    conn.commit()
    conn.close()


def _sent(transport: LogTransport, event: str) -> list[dict]:
    return [s for s in transport.sent if s.get("event") == event]


def _arrive(engine: PushEngine, transport: LogTransport) -> None:
    """Object appears in the zone, then Frigate promotes it to a review."""
    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review()))


# -- the start push ----------------------------------------------------------


def test_present_situation_starts_a_live_activity(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)

    starts = _sent(transport, "start")
    assert len(starts) == 1
    assert starts[0]["token"] == PTS  # the push-to-start token, not the alert one
    aps = starts[0]["payload"]["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == ATTRIBUTES_TYPE
    assert aps["attributes"] == {
        "situation_id": "package-delivery", "situation_name": "Package delivery",
        "camera_id": "doorbell", "handle": starts[0]["payload"]["handle"],
    }
    assert aps["content-state"]["stage"] == "arriving"
    assert set(aps["content-state"]) == {
        "stage", "dwell_seconds", "title", "subtitle", "thumbnail_revision",
    }
    # Present tier must not buzz.
    assert _sent(transport, None) == [] or all(
        s.get("live_activity") for s in transport.sent
    )


def test_activity_starts_before_the_loiter_threshold(tmp_path: Path) -> None:
    """Plan §3: the LA appears when the person enters the zone, at "0:04" --
    the dwell threshold decides the *interrupt*, not the activity."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)  # 0s of dwell, loiter_seconds is 3
    assert len(_sent(transport, "start")) == 1


def test_no_start_for_a_situation_that_does_not_match(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object(zones=("sidewalk",))))
    _run(engine.handle_event(_review(zones=("sidewalk",))))
    assert transport.sent == []


def test_snoozed_situation_does_not_sprout_an_activity(tmp_path: Path) -> None:
    """A Live Activity is still something appearing on the lock screen."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    import time as _time

    conn = db.open_sidecar(db_path)
    store.set_snooze(conn, apns_token="tok", scope="global",
                     until_epoch=_time.time() + 900)
    conn.commit()
    conn.close()

    _arrive(engine, transport)
    assert transport.sent == []


# -- the fallback rule -------------------------------------------------------


def test_device_without_push_to_start_token_gets_an_alert_instead(tmp_path: Path) -> None:
    """Handoff item 9 / "the app works without Phase 2"."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _backdate(engine, 4)
    _run(engine.handle_object_payload(_object()))

    assert _sent(transport, "start") == []
    alerts = [s for s in transport.sent if "situation_id" in s and not s.get("live_activity")]
    assert len(alerts) == 1
    assert alerts[0]["payload"]["situation_id"] == "package-delivery"


def test_v1_device_is_untouched_by_phase_2(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok-v1", cameras=["doorbell"])
    transport = LogTransport()
    engine = _engine(db_path, transport)

    assert _run(engine.handle_event(_review())) == 1
    assert transport.sent[0]["severity"] == "alert"
    assert not transport.sent[0].get("live_activity")


def test_interrupt_tier_situations_do_not_start_activities(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _backdate(engine, 6)
    _run(engine.handle_object_payload(_object()))

    assert _sent(transport, "start") == []
    assert len([s for s in transport.sent if "payload" in s]) == 1  # one alert


# -- updates -----------------------------------------------------------------


def _attach_token(db_path: Path, engine: PushEngine, activity_id: str = "act-1") -> str:
    conn = db.open_sidecar(db_path)
    row = conn.execute("SELECT * FROM push_activities LIMIT 1").fetchone()
    real_id = row["activity_id"]
    store.attach_activity_token(
        conn, activity_id=real_id, apns_token=row["apns_token"],
        situation_id=row["situation_id"], track_id=row["track_id"], token="act-token",
    )
    conn.commit()
    conn.close()
    return real_id


def test_updates_are_silent_and_use_the_per_activity_token(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _backdate(engine, 2)
    _age_pushes(db_path, 4)
    _run(engine.handle_object_payload(_object()))

    updates = _sent(transport, "update")
    assert len(updates) == 1
    assert updates[0]["token"] == "act-token"
    aps = updates[0]["payload"]["aps"]
    assert aps["event"] == "update"
    # Silence is the absence of an alert key -- that is the whole mechanism.
    assert "alert" not in aps
    assert "attributes" not in aps
    assert aps["content-state"]["stage"] == "present"


def test_updates_are_coalesced_to_one_per_three_seconds(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    # A busy object stream: Frigate publishes every ~0.2s.
    for _ in range(20):
        _backdate(engine, 0.2)
        _run(engine.handle_object_payload(_object()))

    assert len(_sent(transport, "update")) <= 2


def test_no_update_before_the_app_uploads_a_token(tmp_path: Path) -> None:
    """iOS mints the per-activity token after the start push; the window where
    an activity is on screen and unaddressable is normal, not an error."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _backdate(engine, 2)
    _run(engine.handle_object_payload(_object()))
    assert _sent(transport, "update") == []


def test_update_budget_is_separate_from_the_alert_ceiling(tmp_path: Path) -> None:
    # No escalation block: the only thing this situation can emit is LA
    # traffic, so the alert ceiling below is measuring what it claims to.
    quiet = {k: v for k, v in PACKAGE_DELIVERY.items() if k != "escalation"}
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[quiet], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport, activity_updates_per_hour=3,
                     activity_update_min_interval_s=0.0)
    _arrive(engine, transport)
    activity_id = _attach_token(db_path, engine)

    for _ in range(10):
        _backdate(engine, 1)
        _run(engine.handle_object_payload(_object()))

    # 1 start + 3 updates, then the LA budget closes.
    assert len(_sent(transport, "update")) <= 3
    conn = db.open_sidecar(db_path)
    # ...and none of it touched the alert tier's 10/hour ceiling.
    assert store.count_sends_since(
        conn, apns_token="tok", situation_id="package-delivery", since=0
    ) == 0
    assert store.count_activity_sends(conn, activity_id=activity_id, since=0) > 0
    conn.close()


# -- escalation --------------------------------------------------------------


def test_escalation_is_one_alert_with_the_activitys_collapse_id(tmp_path: Path) -> None:
    """The non-negotiable: one thing evolving, not two events."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    start_collapse = _sent(transport, "start")[0]["collapse_id"]

    _backdate(engine, 6)  # past the loiter_exceeds:5 bar
    _run(engine.handle_object_payload(_object()))

    alerts = [s for s in transport.sent if not s.get("live_activity") and "payload" in s]
    assert len(alerts) == 1
    assert alerts[0]["collapse_id"] == start_collapse
    assert alerts[0]["payload"]["content_state"]["stage"] == "escalated"

    # And it does not fire again on the next observation.
    _run(engine.handle_object_payload(_object()))
    assert len([s for s in transport.sent if not s.get("live_activity") and "payload" in s]) == 1


def test_no_escalation_without_an_escalation_block(tmp_path: Path) -> None:
    """A Present situation the user never asked to escalate is a thing you
    watch, not a thing that eventually buzzes."""
    quiet = dict(PACKAGE_DELIVERY)
    quiet.pop("escalation")
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[quiet], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _backdate(engine, 60)
    _run(engine.handle_object_payload(_object()))

    assert [s for s in transport.sent if not s.get("live_activity")] == []


def test_audio_event_escalation(tmp_path: Path) -> None:
    ring = dict(PACKAGE_DELIVERY)
    ring["audio_events"] = ["doorbell"]
    ring["escalation"] = {"from_tier": "present", "to_tier": "interrupt",
                          "on": "audio_event"}
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[ring], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _run(engine.handle_event(_review(audio=("doorbell",))))

    alerts = [s for s in transport.sent if not s.get("live_activity") and "payload" in s]
    assert len(alerts) == 1


def test_sub_label_unknown_escalation(tmp_path: Path) -> None:
    unknown = dict(PACKAGE_DELIVERY)
    unknown["escalation"] = {"from_tier": "present", "to_tier": "interrupt",
                             "on": "sub_label_unknown"}
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[unknown], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    # A recognised face suppresses the escalation...
    _run(engine.handle_object_payload(_object(sub_label="alice")))
    _run(engine.handle_event(_review()))
    _attach_token(db_path, engine)
    _backdate(engine, 2)
    _run(engine.handle_object_payload(_object(sub_label="alice")))
    assert [s for s in transport.sent if not s.get("live_activity")] == []

    # ...an unrecognised one does not.
    engine._sub_labels.clear()  # noqa: SLF001
    _backdate(engine, 2)
    _run(engine.handle_object_payload(_object()))
    assert len([s for s in transport.sent if not s.get("live_activity")]) == 1


# -- resolution --------------------------------------------------------------


def test_leaving_the_zone_ends_the_activity_with_a_tail(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _run(engine.handle_object_payload(_object(zones=())))  # stepped out

    ends = _sent(transport, "end")
    assert len(ends) == 1
    aps = ends[0]["payload"]["aps"]
    assert aps["event"] == "end"
    assert aps["content-state"]["stage"] == "ending"
    assert aps["dismissal-date"] - aps["timestamp"] == 30


def test_frigate_ending_the_object_ends_the_activity(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _run(engine.handle_object_payload(_object(type_="end")))

    assert len(_sent(transport, "end")) == 1


def test_sweeper_ends_an_activity_that_went_quiet(tmp_path: Path) -> None:
    """Resolution is the one transition no message announces."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport, activity_resolution_s=30.0)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    assert _sent(transport, "end") == []

    import time as _time

    assert _run(engine.sweep_activities(now=_time.time() + 31)) == 1
    assert len(_sent(transport, "end")) == 1


def test_sweeper_leaves_a_live_activity_alone(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    assert _run(engine.sweep_activities()) == 0
    assert _sent(transport, "end") == []


def test_ended_activity_rows_are_reaped(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    engine = _engine(db_path, activity_reap_after_s=300.0)
    _arrive(engine, engine.transport)  # type: ignore[arg-type]
    _attach_token(db_path, engine)
    _run(engine.handle_object_payload(_object(type_="end")))

    import time as _time

    conn = db.open_sidecar(db_path)
    assert conn.execute("SELECT COUNT(*) FROM push_activities").fetchone()[0] == 1
    assert store.reap_activities(conn, older_than=300.0, now=_time.time() + 400) == 1
    assert conn.execute("SELECT COUNT(*) FROM push_activities").fetchone()[0] == 0
    conn.close()


# -- early fire --------------------------------------------------------------


def test_early_fire_starts_on_a_detection_review(tmp_path: Path) -> None:
    """Plan §4 lever 5: `detection` arrives ~500ms before the `alert`."""
    early = dict(PACKAGE_DELIVERY)
    early["detection_tier_early_fire"] = True
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[early], schema_version=2,
              push_to_start_token=PTS)  # min_severity defaults to "alert"
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review(severity="detection")))
    assert len(_sent(transport, "start")) == 1


def test_without_the_opt_in_a_detection_review_starts_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review(severity="detection")))
    assert transport.sent == []


def test_unpromoted_early_fire_ends_with_the_short_tail(tmp_path: Path) -> None:
    early = dict(PACKAGE_DELIVERY)
    early["detection_tier_early_fire"] = True
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[early], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review(severity="detection")))
    _attach_token(db_path, engine)
    _run(engine.handle_object_payload(_object(type_="end")))

    aps = _sent(transport, "end")[0]["payload"]["aps"]
    # A guess that never panned out leaves quickly.
    assert aps["dismissal-date"] - aps["timestamp"] == 10


def test_promoted_early_fire_ends_with_the_full_tail(tmp_path: Path) -> None:
    early = dict(PACKAGE_DELIVERY)
    early["detection_tier_early_fire"] = True
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[early], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review(severity="detection")))
    _attach_token(db_path, engine)
    _run(engine.handle_event(_review(severity="alert")))  # Frigate promoted it
    _run(engine.handle_object_payload(_object(type_="end")))

    aps = _sent(transport, "end")[0]["payload"]["aps"]
    assert aps["dismissal-date"] - aps["timestamp"] == 30


# -- a dead activity token is not a dead device ------------------------------


class _DeadActivityTokenTransport(LogTransport):
    async def send_live_activity(self, device, *, token, payload, collapse_id, event):
        if event == "update":
            return TransportResult(ok=False, unregistered=True, error="410")
        return await super().send_live_activity(
            device, token=token, payload=payload, collapse_id=collapse_id, event=event
        )


def test_dead_activity_token_closes_the_activity_not_the_device(tmp_path: Path) -> None:
    """410 on an update means iOS tore the activity down -- the user swiped it
    away. Pruning the device row for that would unregister a good phone."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = _DeadActivityTokenTransport()
    engine = _engine(db_path, transport)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _backdate(engine, 2)
    _age_pushes(db_path, 4)
    _run(engine.handle_object_payload(_object()))

    conn = db.open_sidecar(db_path)
    assert len(store.list_devices(conn)) == 1  # device survives
    row = conn.execute("SELECT ended_at FROM push_activities").fetchone()
    conn.close()
    assert row["ended_at"] is not None  # activity closed


# -- routes ------------------------------------------------------------------


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000",
                               config_path=fake_config, db_path=frigate_db_path),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001,
                               require_frigate_auth=False),
        push=PushSection(enabled=False),
    )
    return TestClient(create_app(settings))


def test_registration_accepts_and_echoes_the_push_to_start_token(client: TestClient) -> None:
    r = client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox",
        "situations": [PACKAGE_DELIVERY], "push_to_start_token": PTS,
    })
    assert r.status_code == 200
    assert r.json()["live_activities"] is True


def test_registration_without_the_token_says_so(client: TestClient) -> None:
    r = client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox", "situations": [PACKAGE_DELIVERY],
    })
    assert r.json()["live_activities"] is False


def test_reregistering_without_the_token_keeps_the_stored_one(client: TestClient) -> None:
    """The app uploads it off an async token stream, so the first PUT after
    launch can legitimately race ahead of the token arriving."""
    client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox", "push_to_start_token": PTS,
    })
    r = client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox",
    })
    assert r.json()["live_activities"] is True


def test_activity_token_upload_round_trip(client: TestClient, sidecar_db_path: Path) -> None:
    client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox",
        "situations": [PACKAGE_DELIVERY], "push_to_start_token": PTS,
    })
    r = client.post("/v1/push/activity/token", json={
        "apns_token": "tok", "situation_id": "package-delivery", "track_id": "t1",
        "activity_id": "act-1", "token": "per-activity-token",
    })
    assert r.status_code == 200 and r.json()["accepted"] is True

    conn = db.open_sidecar(sidecar_db_path)
    row = store.find_activity(
        conn, apns_token="tok", situation_id="package-delivery", track_id="t1"
    )
    conn.close()
    assert row is not None and row["token"] == "per-activity-token"


def test_activity_token_upload_for_unknown_device_is_404(client: TestClient) -> None:
    r = client.post("/v1/push/activity/token", json={
        "apns_token": "nope", "situation_id": "s", "track_id": "t",
        "activity_id": "a", "token": "x",
    })
    assert r.status_code == 404


def test_deleting_an_activity_token_is_idempotent(
    client: TestClient, sidecar_db_path: Path
) -> None:
    client.put("/v1/push/devices/tok", json={
        "bundle_id": "com.x", "environment": "sandbox", "push_to_start_token": PTS,
    })
    client.post("/v1/push/activity/token", json={
        "apns_token": "tok", "situation_id": "s", "track_id": "t",
        "activity_id": "act-1", "token": "x",
    })
    first = client.delete("/v1/push/activity/token/act-1")
    second = client.delete("/v1/push/activity/token/act-1")
    assert first.json()["was_tracked"] is True
    assert second.status_code == 200 and second.json()["was_tracked"] is False


# -- escalation parsing ------------------------------------------------------


def test_escalation_parses_the_three_section_8_forms() -> None:
    for on, kind, threshold in [
        ("loiter_exceeds:5", "loiter_exceeds", 5.0),
        ("audio_event", "audio_event", 0.0),
        ("sub_label_unknown", "sub_label_unknown", 0.0),
    ]:
        rule = Escalation.from_dict({"from_tier": "present", "to_tier": "interrupt", "on": on})
        assert rule is not None
        assert (rule.kind, rule.threshold) == (kind, threshold)


def test_unrecognised_escalation_trigger_is_ignored_not_fatal() -> None:
    assert Escalation.from_dict({"on": "vibes"}) is None
    assert Escalation.from_dict(None) is None


def test_bare_loiter_exceeds_falls_back_to_the_situations_own_loiter() -> None:
    """`{"on": "loiter_exceeds"}` with no number must not be instantly true."""
    from frigate_sidecar.push.situations import escalation_reached

    situation = Situation(
        id="x", name="X", loiter_seconds=8.0,
        escalation=Escalation(kind="loiter_exceeds", threshold=0.0),
    )
    below = Match(situation=situation, track_id="t", dwell_s=4.0, label="person", zone="z")
    above = Match(situation=situation, track_id="t", dwell_s=9.0, label="person", zone="z")
    assert escalation_reached(below) is False
    assert escalation_reached(above) is True


def test_rate_limited_escalation_is_consumed_not_retried(tmp_path: Path) -> None:
    """A retryable escalation would re-gate on every object message -- several
    a second -- and each rate-limited call bumps the suppressed counter, so a
    situation that buzzed once ends up claiming "+2000 more"."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[PACKAGE_DELIVERY], schema_version=2,
              push_to_start_token=PTS)
    transport = LogTransport()
    engine = _engine(db_path, transport, rate_limit_per_hour=0)

    _arrive(engine, transport)
    _attach_token(db_path, engine)
    _backdate(engine, 6)
    for _ in range(25):
        _run(engine.handle_object_payload(_object()))

    assert [s for s in transport.sent if not s.get("live_activity")] == []
    conn = db.open_sidecar(db_path)
    suppressed = store.take_suppressed(
        conn, apns_token="tok", situation_id="package-delivery"
    )
    conn.close()
    assert suppressed == 1
