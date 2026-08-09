"""Integration tests for the card-model Live Activity lifecycle wired into
`push/delivery_wire.py` (Elsinore Phase 3): push-to-start on a qualifying
create, content-state updates via the per-activity token, and end on
resolve -- run through the same `handle_delivery_event`/
`handle_delivery_resolve` entry points the ordinary card push uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection
from frigate_sidecar.push import store
from frigate_sidecar.push.delivery_wire import handle_delivery_event, handle_delivery_resolve
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.transport import LogTransport


def make_device(token: str = "tok1", *, push_to_start: str = "pts1") -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", push_to_start_token=push_to_start,
    )


def make_event(camera: str, track_id: str, label: str, zones: tuple[str, ...] = ()) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=(label,), track_ids=(track_id,), zones=zones,
    )


def la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


def find_activity_row(conn, *, apns_token: str, card_key: str, track_id: str):
    return conn.execute(
        "SELECT * FROM push_activities WHERE apns_token = ? AND situation_id = ? "
        "AND track_id = ?",
        (apns_token, card_key, track_id),
    ).fetchone()


def attach_token(conn, *, device: Device, card_key: str, track_id: str, token: str) -> None:
    row = find_activity_row(
        conn, apns_token=device.apns_token, card_key=card_key, track_id=track_id,
    )
    assert row is not None
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=device.apns_token,
        situation_id=card_key, track_id=track_id, token=token,
    )


@pytest.mark.asyncio
async def test_full_la_lifecycle_create_enrich_escalate_resolve(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:thing:trk1"

    # create: package, no zone -> log
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 1
    assert sends[0]["event"] == "start"
    assert sends[0]["token"] == "pts1"
    start_state = sends[0]["payload"]["aps"]["content-state"]
    assert start_state["mutation"] == "create"
    assert start_state["glyph"] == "shippingbox.fill"
    assert sends[0]["payload"]["aps"]["attributes"]["family"] == "package"

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perActivity1")

    # enrich: same input again, level unchanged
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 2
    assert sends[1]["event"] == "update"
    assert sends[1]["token"] == "perActivity1"
    enrich_state = sends[1]["payload"]["aps"]["content-state"]
    assert enrich_state["mutation"] == "enrich"
    assert enrich_state["elapsed_seconds"] == 10

    # escalate: gains a zone, log -> quiet is a level rise
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("porch",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 3
    assert sends[2]["event"] == "update"
    escalate_state = sends[2]["payload"]["aps"]["content-state"]
    assert escalate_state["mutation"] == "escalate"
    assert escalate_state["elapsed_seconds"] == 0  # state_since_at reset on escalate

    # resolve
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=30.0,
    )
    assert resolved == 1
    sends = la_sends(transport)
    assert len(sends) == 4
    assert sends[3]["event"] == "end"
    end_payload = sends[3]["payload"]["aps"]
    assert end_payload["dismissal-date"] == 34
    assert end_payload["content-state"]["mutation"] == "resolve"
    assert end_payload["content-state"]["glyph"] == "checkmark.circle.fill"
    assert end_payload["content-state"]["thumbnail_handle"] is None

    row = find_activity_row(conn, apns_token=device.apns_token, card_key=card_key, track_id="trk1")
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_non_qualifying_card_gets_no_live_activity(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("yard-cam", "trk1", "dog", zones=("yard",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []


@pytest.mark.asyncio
async def test_no_push_to_start_token_skips_la_but_card_push_still_sent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device(push_to_start="")  # LA not enabled on this device
    config = PushSection(delivery_enabled=True)

    mutated = await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert mutated == 1
    assert la_sends(transport) == []
    assert conn.execute("SELECT COUNT(*) FROM push_cards").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_late_per_activity_token_drops_update_until_it_arrives(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:thing:trk1"

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # No token uploaded yet -- the enrich update is silently dropped, not
    # buffered, per the design doc's documented tradeoff.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )
    assert len(la_sends(transport)) == 1  # only the start

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="late-token")
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("porch",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 2
    assert sends[1]["event"] == "update"
    assert sends[1]["token"] == "late-token"


@pytest.mark.asyncio
async def test_resolve_before_token_arrives_ends_via_push_to_start_token(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=5.0,
    )
    assert resolved == 1
    sends = la_sends(transport)
    assert len(sends) == 2
    assert sends[1]["event"] == "end"
    assert sends[1]["token"] == "pts1"  # push-to-start, since no per-activity token ever arrived


@pytest.mark.asyncio
async def test_cross_camera_dedup_only_surviving_card_gets_a_live_activity(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("cam-a", "trkA", "package", zones=("driveway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", "package", zones=("driveway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_delivery_la_enabled_false_suppresses_all_live_activities(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True, delivery_la_enabled=False)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []


@pytest.mark.asyncio
async def test_settings_family_toggle_suppresses_just_that_family(sidecar_db_path: Path):
    """Elsinore Phase 4: `settings.live_activities.<family>` gates family
    detection, one layer below `delivery_la_enabled`'s whole-feature kill
    switch above."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    disabled = policy_settings.default_settings()
    disabled["live_activities"]["package"] = False
    policy_settings.apply_settings(disabled)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []
    # An ordinary card push still went out -- only the LA family is off.
    assert conn.execute("SELECT COUNT(*) FROM push_cards").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_settings_opening_picks_restrict_which_openings_get_an_activity(
    sidecar_db_path: Path,
):
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    curated = policy_settings.default_settings()
    curated["live_activities"]["opening_picks"] = ["front_gate"]
    policy_settings.apply_settings(curated)

    # Not on the curated list -- no activity.
    await handle_delivery_event(
        make_event("side-cam", "trk1", "garage"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []

    # On the curated list (matches the camera name) -- activity starts.
    await handle_delivery_event(
        make_event("front_gate", "trk2", "gate"),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
