"""End-to-end situation dispatch: the handoff's "what done looks like" list.

Every test here maps to a line of it -- backward compatibility, silence where
silence is due, one push per dwell, collapse behaviour, the rate-limit
ceiling, snooze, and a push that survives Frigate refusing to hand over a
snapshot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from frigate_sidecar import db
from frigate_sidecar.push import store
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.transport import LogTransport, TransportResult

NOW = 1_785_000_000.0

AT_THE_DOOR: dict[str, Any] = {
    "id": "at-the-door", "name": "At the door", "tier": "interrupt",
    "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
    "loiter_seconds": 5, "sound": "chime",
}
NEAR_MY_CAR: dict[str, Any] = {
    "id": "near-my-car", "name": "Near my car", "tier": "interrupt",
    "cameras": ["driveway"], "labels": ["person"], "zones": ["driveway"],
    "loiter_seconds": 8,
}


def _engine(db_path: Path, transport=None, **kw) -> PushEngine:
    return PushEngine(
        db_path=str(db_path), transport=transport or LogTransport(), server_id="s1",
        # No Frigate to reach in unit tests: the pre-warm is skipped, which is
        # exactly the "snapshot unavailable" path and must not block a push.
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


def _object(track_id: str = "t1", camera: str = "doorbell", zones=("porch",), type_="update"):
    return {
        "type": type_,
        "after": {"id": track_id, "camera": camera, "label": "person",
                  "current_zones": list(zones), "entered_zones": list(zones)},
    }


def _run(coro):
    return asyncio.run(coro)


# -- backward compatibility (handoff item 2, the "must ship in one deploy") --


def test_v1_device_with_no_situations_still_gets_todays_push(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok-v1", cameras=["doorbell"])
    transport = LogTransport()
    engine = _engine(db_path, transport)

    sent = _run(engine.handle_event(_review()))
    assert sent == 1
    # The v1 wire shape, unchanged: a handle and a severity, no situation.
    assert transport.sent[0]["severity"] == "alert"
    assert "situation_id" not in transport.sent[0]


def test_v1_and_v2_devices_coexist_on_one_event(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok-v1")
    _register(db_path, "tok-v2", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    sent = _run(engine.handle_event(_review()))

    # The v1 phone is told immediately; the v2 phone is still waiting on dwell.
    assert sent == 1
    assert [s.get("situation_id", "") for s in transport.sent] == [""]


def test_empty_situations_list_is_the_v1_path(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[], schema_version=2)
    conn = db.open_sidecar(db_path)
    device = store.get_device(conn, "tok")
    conn.close()
    assert device is not None and device.uses_situations is False


# -- the situation path ------------------------------------------------------


def test_dwell_threshold_fires_exactly_one_push(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    # Person arrives; Frigate publishes the object, then the review item.
    _run(engine.handle_object_payload(_object()))
    assert _run(engine.handle_event(_review())) == 0  # 0s of dwell so far

    # Object updates tick past the 5s threshold.
    for state in engine.tracks._tracks.values():  # noqa: SLF001 - backdating the clock
        state.first_seen_in_zone["porch"] -= 6
    assert _run(engine.handle_object_payload(_object())) == 1
    # ...and keeps ticking, without buzzing again.
    assert _run(engine.handle_object_payload(_object())) == 0
    assert len(transport.sent) == 1

    payload = transport.sent[0]["payload"]
    assert payload["situation_id"] == "at-the-door"
    assert payload["aps"]["alert"]["title"] == "At the door"
    assert payload["aps"]["alert"]["body"].startswith("Person, ")
    assert payload["aps"]["category"] == "situation.at-the-door"
    assert payload["aps"]["thread-id"] == "at-the-door"
    assert payload["aps"]["interruption-level"] == "time-sensitive"
    assert payload["aps"]["mutable-content"] == 1
    assert payload["aps"]["sound"] == "chime.caf"
    assert payload["handle"].startswith("h_")
    # Collapse id is a header value, keyed on situation + track (plan §8).
    assert transport.sent[0]["collapse_id"] == "at-the-door:t1"


def test_object_stream_alone_never_pushes(tmp_path: Path) -> None:
    """`frigate/reviews` stays the sole authority on push-worthiness."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    for _ in range(50):
        _run(engine.handle_object_payload(_object()))
    assert transport.sent == []


def test_no_matching_situation_is_silent(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[NEAR_MY_CAR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review()))
    assert transport.sent == []


def test_two_tracks_produce_two_notifications(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    _run(engine.handle_object_payload(_object("t1")))
    _run(engine.handle_object_payload(_object("t2")))
    _run(engine.handle_event(_review(track_ids=("t1", "t2"))))
    for state in engine.tracks._tracks.values():  # noqa: SLF001
        state.first_seen_in_zone["porch"] -= 6
    _run(engine.handle_object_payload(_object("t1")))
    _run(engine.handle_object_payload(_object("t2")))

    assert {s["collapse_id"] for s in transport.sent} == {
        "at-the-door:t1", "at-the-door:t2"
    }


def test_frigate_restart_wipes_track_state(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    engine = _engine(db_path)
    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review()))
    assert len(engine.tracks) == 1

    engine.reset_tracks()
    assert len(engine.tracks) == 0
    # The pending review goes with it -- its track id means nothing now.
    assert engine._pending == {}  # noqa: SLF001


def test_object_end_forgets_the_track(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    engine = _engine(db_path)
    _run(engine.handle_object_payload(_object()))
    _run(engine.handle_event(_review()))
    _run(engine.handle_object_payload(_object(type_="end")))
    assert len(engine.tracks) == 0 and engine._pending == {}  # noqa: SLF001


# -- rate limiting (plan §6) -------------------------------------------------


def _fire_n(engine: PushEngine, n: int, db_path: Path, *, prefix: str = "t") -> None:
    """n independent dwells, as a stuck camera would produce.

    `prefix` keeps track ids unique across calls -- a track that already fired
    stays fired for its whole life, so reusing an id would test the
    once-per-dwell rule rather than whatever the caller meant.
    """
    for i in range(n):
        track = f"{prefix}{i}"
        _run(engine.handle_object_payload(_object(track)))
        _run(engine.handle_event(_review(review_id=f"r-{track}", track_ids=(track,))))
        for state in engine.tracks._tracks.values():  # noqa: SLF001
            state.first_seen_in_zone["porch"] -= 6
        _run(engine.handle_object_payload(_object(track)))


def test_runaway_camera_is_capped_then_reports_what_it_swallowed(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport, rate_limit_per_hour=10)

    _fire_n(engine, 40, db_path)
    assert len(transport.sent) == 10  # 30 suppressed

    # Window opens: the next qualifying push carries what was missed.
    conn = db.open_sidecar(db_path)
    conn.execute("UPDATE push_sends SET sent_at = sent_at - 4000")
    conn.commit()
    conn.close()
    _fire_n(engine, 1, db_path, prefix="later")

    assert len(transport.sent) == 11
    assert transport.sent[-1]["payload"]["aps"]["alert"]["body"].endswith(" · +30 more")


def test_suppressed_suffix_is_spent_once(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport, rate_limit_per_hour=1)

    def _open_the_window() -> None:
        conn = db.open_sidecar(db_path)
        conn.execute("UPDATE push_sends SET sent_at = sent_at - 4000")
        conn.commit()
        conn.close()

    _fire_n(engine, 3, db_path, prefix="a")  # 1 sent, 2 suppressed
    _open_the_window()
    _fire_n(engine, 1, db_path, prefix="b")  # spends the +2
    _open_the_window()
    _fire_n(engine, 1, db_path, prefix="c")  # nothing left to report

    bodies = [s["payload"]["aps"]["alert"]["body"] for s in transport.sent]
    assert bodies[1].endswith(" · +2 more")
    assert "more" not in bodies[2]


def test_rate_limit_is_per_situation_and_per_device(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    conn = db.open_sidecar(db_path)
    store.record_send(conn, apns_token="tokA", situation_id="at-the-door", now=NOW)
    conn.commit()
    assert store.count_sends_since(
        conn, apns_token="tokA", situation_id="at-the-door", since=NOW - 3600
    ) == 1
    assert store.count_sends_since(
        conn, apns_token="tokB", situation_id="at-the-door", since=NOW - 3600
    ) == 0
    assert store.count_sends_since(
        conn, apns_token="tokA", situation_id="near-my-car", since=NOW - 3600
    ) == 0
    conn.close()


# -- snooze (plan §6) --------------------------------------------------------


@pytest.mark.parametrize(
    "scope", ["situation:at-the-door", "camera:doorbell", "global"]
)
def test_every_snooze_scope_silences_the_push(tmp_path: Path, scope: str) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    import time as _time

    conn = db.open_sidecar(db_path)
    store.set_snooze(conn, apns_token="tok", scope=scope, until_epoch=_time.time() + 900)
    conn.commit()
    conn.close()

    _fire_n(engine, 1, db_path)
    assert transport.sent == []


def test_snooze_re_enables_itself_at_expiry(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    import time as _time

    conn = db.open_sidecar(db_path)
    store.set_snooze(
        conn, apns_token="tok", scope="global", until_epoch=_time.time() - 1
    )
    conn.commit()
    conn.close()

    _fire_n(engine, 1, db_path)
    assert len(transport.sent) == 1


def test_a_snoozed_match_does_not_burn_the_dwell(tmp_path: Path) -> None:
    """Snooze is "not now", not "consider this handled" -- but it also must
    not leave the track primed to fire the instant the snooze lifts, which
    would deliver a stale buzz about somebody long gone."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    engine = _engine(db_path, transport)

    import time as _time

    conn = db.open_sidecar(db_path)
    store.set_snooze(conn, apns_token="tok", scope="global", until_epoch=_time.time() + 900)
    conn.commit()
    conn.close()

    _fire_n(engine, 1, db_path)
    assert transport.sent == []
    # Nothing was sent, so nothing was charged against the hourly ceiling.
    conn = db.open_sidecar(db_path)
    assert store.count_sends_since(
        conn, apns_token="tok", situation_id="at-the-door", since=0
    ) == 0
    conn.close()


# -- failure paths -----------------------------------------------------------


class _FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, device, **kw):
        return TransportResult(ok=False, error="503")

    async def send_situation(self, device, *, payload, collapse_id):
        self.calls += 1
        return TransportResult(ok=False, error="503 relay unreachable")


def test_transport_failure_leaves_the_dwell_retryable(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = _FailingTransport()
    engine = _engine(db_path, transport)

    _fire_n(engine, 1, db_path)
    assert transport.calls == 1
    # A failed send must not count as "this dwell already fired", or a relay
    # blip would silently eat the notification for good.
    _run(engine.handle_object_payload(_object("t0")))
    assert transport.calls == 2
    conn = db.open_sidecar(db_path)
    assert store.count_sends_since(
        conn, apns_token="tok", situation_id="at-the-door", since=0
    ) == 0
    conn.close()


class _SlowTransport:
    """A send that takes long enough for the next object message to arrive."""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, device, **kw):
        return TransportResult(ok=True)

    async def send_situation(self, device, *, payload, collapse_id):
        self.calls += 1
        await asyncio.sleep(0.05)
        return TransportResult(ok=True)


def test_a_second_update_mid_send_does_not_double_push(tmp_path: Path) -> None:
    """Object messages arrive every few hundred ms and a send takes longer.

    If the once-per-dwell claim were taken after the send returned, the update
    that lands while the first push is still in flight would match the same
    dwell again and buzz twice.
    """
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = _SlowTransport()
    engine = _engine(db_path, transport)

    async def scenario() -> None:
        await engine.handle_object_payload(_object())
        await engine.handle_event(_review())
        for state in engine.tracks._tracks.values():  # noqa: SLF001
            state.first_seen_in_zone["porch"] -= 6
        # Two updates racing, exactly as the live stream delivers them.
        await asyncio.gather(
            engine.handle_object_payload(_object()),
            engine.handle_object_payload(_object()),
        )

    _run(scenario())
    assert transport.calls == 1


class _DeadTokenTransport:
    async def send(self, device, **kw):
        return TransportResult(ok=False, unregistered=True, error="410")

    async def send_situation(self, device, *, payload, collapse_id):
        return TransportResult(ok=False, unregistered=True, error="410 Unregistered")


def test_410_on_a_situation_push_prunes_the_device(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    engine = _engine(db_path, _DeadTokenTransport())

    _fire_n(engine, 1, db_path)
    conn = db.open_sidecar(db_path)
    assert store.list_devices(conn) == []
    conn.close()


def test_push_fires_even_when_no_snapshot_can_be_fetched(tmp_path: Path) -> None:
    """Handoff item 12 / the last line of "done looks like"."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok", situations=[AT_THE_DOOR], schema_version=2)
    transport = LogTransport()
    # A base URL that resolves to nothing: every pre-warm attempt fails.
    engine = _engine(db_path, transport)
    engine.frigate_base_url = "http://127.0.0.1:1/"
    engine.thumbnail_timeout_s = 0.25

    _fire_n(engine, 1, db_path)
    assert len(transport.sent) == 1

    handle = transport.sent[0]["payload"]["handle"]
    conn = db.open_sidecar(db_path)
    assert store.get_thumbnail(conn, handle) is None
    conn.close()


# -- persistence of the v2 record -------------------------------------------


def test_ignored_fields_are_persisted_for_later_phases(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(
        db_path, "tok",
        situations=[AT_THE_DOOR], schema_version=2,
        timezone_name="America/Los_Angeles",
        location={"lat": 45.51, "lon": -122.68},
        live_activity_token="la-token",
        morning_digest={"enabled": True, "hour": 7, "minute": 0},
        llm={"mode": "cloud", "endpoint": "anthropic", "api_key": "sk-x"},
    )
    conn = db.open_sidecar(db_path)
    row = conn.execute("SELECT * FROM push_devices WHERE apns_token = 'tok'").fetchone()
    device = store.get_device(conn, "tok")
    conn.close()

    assert json.loads(row["morning_digest"])["hour"] == 7
    assert json.loads(row["llm"])["mode"] == "cloud"
    assert row["live_activity_token"] == "la-token"
    assert device is not None
    assert device.timezone == "America/Los_Angeles"
    assert device.location == (45.51, -122.68)
    assert device.schema_version == 2


def test_upgrading_an_existing_v1_database_keeps_its_rows(tmp_path: Path) -> None:
    """The migration runs against a DB created before the v2 columns existed."""
    db_path = tmp_path / "sidecar.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE push_devices ("
        " apns_token TEXT PRIMARY KEY, device_id TEXT NOT NULL, bundle_id TEXT NOT NULL,"
        " environment TEXT NOT NULL, app_version TEXT NOT NULL DEFAULT '',"
        " cameras TEXT NOT NULL DEFAULT '[]', labels TEXT NOT NULL DEFAULT '[]',"
        " min_severity TEXT NOT NULL DEFAULT 'alert', registered_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL);"
        "INSERT INTO push_devices VALUES"
        " ('old-tok','d_old','com.x','sandbox','1.0','[\"doorbell\"]','[]','alert','t','t');"
    )
    conn.commit()
    conn.close()
    db._SCHEMA_APPLIED.discard(str(db_path.resolve()))  # noqa: SLF001

    conn = db.open_sidecar(db_path)
    device = store.get_device(conn, "old-tok")
    conn.close()
    assert device is not None
    assert device.cameras == ("doorbell",)
    assert device.uses_situations is False  # still the v1 path
    assert device.situations == ()


def test_device_type_is_hashable_free_of_dict_fields() -> None:
    """`Device` is frozen; a dict field would make it unhashable and break any
    future use in a set without a word of warning."""
    hash(Device(apns_token="t", device_id="d", bundle_id="b", environment="sandbox"))
