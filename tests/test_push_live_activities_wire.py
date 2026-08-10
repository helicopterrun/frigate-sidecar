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
        min_severity="detection",
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

    # create: package at pool zone -> thing/off_limits = quiet (pushable)
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
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
    assert "relevance-score" in sends[0]["payload"]["aps"]
    assert "stale-date" in sends[0]["payload"]["aps"]
    assert sends[0]["payload"]["aps"]["alert"]["title"]
    assert sends[0]["payload"]["aps"]["alert"]["body"]

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perActivity1")

    # enrich: same input again, level unchanged
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 2
    assert sends[1]["event"] == "update"
    assert sends[1]["token"] == "perActivity1"
    enrich_state = sends[1]["payload"]["aps"]["content-state"]
    assert enrich_state["mutation"] == "enrich"
    assert enrich_state["elapsed_seconds"] == 10

    # resolve
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=30.0,
    )
    assert resolved == 1
    sends = la_sends(transport)
    assert len(sends) == 3
    assert sends[2]["event"] == "end"
    end_payload = sends[2]["payload"]["aps"]
    assert end_payload["dismissal-date"] == 60  # 30s dismissal
    assert end_payload["content-state"]["mutation"] == "resolve"
    assert end_payload["content-state"]["glyph"] == "checkmark.circle.fill"
    assert "thumbnail_handle" not in end_payload["content-state"]

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
        make_event("doorbell", "trk1", "package", zones=("pool",)),
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
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # No token uploaded yet -- the enrich update is silently dropped, not
    # buffered, per the design doc's documented tradeoff.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
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
async def test_resolve_before_token_arrives_skips_end_push(sidecar_db_path: Path):
    """End pushes must NOT fall back to the push-to-start token — iOS rejects
    update/end on the p2s token. If no per-activity token has arrived yet,
    the end push is simply skipped (the activity row is still closed)."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=5.0,
    )
    assert resolved == 1
    sends = la_sends(transport)
    assert len(sends) == 1  # only the start; no end sent without per-activity token


@pytest.mark.asyncio
async def test_cross_camera_dedup_only_surviving_card_gets_a_live_activity(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("cam-a", "trkA", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", "package", zones=("pool",)),
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
        make_event("doorbell", "trk1", "package", zones=("pool",)),
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
        make_event("doorbell", "trk1", "package", zones=("pool",)),
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
        make_event("side-cam", "trk1", "garage", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []

    # On the curated list (matches the camera name) -- activity starts.
    # now=20.0 puts it outside the cross-camera dedup window (15s), ensuring
    # a fresh card rather than an alias to the first event's card.
    await handle_delivery_event(
        make_event("front_gate", "trk2", "gate", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1


def card_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if not r.get("live_activity") and not r.get("test")]


@pytest.mark.asyncio
async def test_card_push_demoted_to_silent_while_la_confirmed(sidecar_db_path: Path):
    """§2: the LA is the alerting surface once it demonstrably exists — the
    card push still goes out (NC history) but passive and soundless, so there
    is exactly one banner per mutation."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:stranger:trk1"

    # create: person at front_door -> notify; LA start accepted by the mock
    # transport, so the create card push is demoted.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport)[0]["event"] == "start"
    create_cards = card_sends(transport)
    assert len(create_cards) == 1
    aps = create_cards[0]["payload"]["aps"]
    assert aps["interruption-level"] == "passive"
    assert "sound" not in aps

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perActivity1")

    # escalate: person moves to pool (off_limits -> urgent). LA update lands
    # on the confirmed token and carries the escalation alert; the card push
    # stays silent.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    last_la = la_sends(transport)[-1]
    assert last_la["event"] == "update"
    assert last_la["payload"]["aps"].get("alert") is not None
    esc_aps = card_sends(transport)[-1]["payload"]["aps"]
    assert esc_aps["interruption-level"] == "passive"
    assert "sound" not in esc_aps


@pytest.mark.asyncio
async def test_card_push_not_demoted_when_la_unconfirmed(sidecar_db_path: Path):
    """The failure mode that forced the eaac866 revert: an LA that never
    materializes must not eat the banner. A device with no push-to-start
    token gets a full-fat card push; so does an escalate whose LA row has
    no per-activity token yet."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    no_pts = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[no_pts], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []
    aps = card_sends(transport)[0]["payload"]["aps"]
    assert aps["interruption-level"] == "active"  # notify level, undemoted
    assert aps.get("sound")

    # Second device: LA starts but the app never uploads a per-activity
    # token — the escalate card push must stay a real banner.
    transport2 = LogTransport()
    device = make_device(token="tok2")
    await handle_delivery_event(
        make_event("porch", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport2, config=config, now=100.0,
    )
    assert la_sends(transport2)[0]["event"] == "start"
    await handle_delivery_event(
        make_event("porch", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport2, config=config, now=110.0,
    )
    esc_aps = card_sends(transport2)[-1]["payload"]["aps"]
    assert esc_aps["interruption-level"] == "time-sensitive"  # urgent, undemoted
    assert esc_aps.get("sound")
    # And the token-less row was kept alive for the sweeper.
    row = find_activity_row(
        conn, apns_token=device.apns_token, card_key="porch:stranger:trkB", track_id="trkB",
    )
    assert row is not None and row["ended_at"] is None


@pytest.mark.asyncio
async def test_la_start_sound_omitted_when_sounds_muted(sidecar_db_path: Path):
    """The LA start keeps its required alert dict but honors the card path's
    sound accounting: with mute_sounds on, the start push carries no sound."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    muted = policy_settings.default_settings()
    muted["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "mute_sounds"}
    policy_settings.apply_settings(muted)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    start = la_sends(transport)[0]
    assert start["event"] == "start"
    assert start["payload"]["aps"].get("alert") is not None
    assert "sound" not in start["payload"]["aps"]


@pytest.mark.asyncio
async def test_deferred_end_when_token_arrives_after_resolve(sidecar_db_path: Path):
    """Fast create→resolve: no per-activity token at resolve time leaves the
    row open (pending_end); the token-upload path then ends the activity via
    end_activity_if_card_closed instead of stranding it."""
    from frigate_sidecar.push.delivery_wire import end_activity_if_card_closed

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:thing:trk1"

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=5.0,
    )
    assert resolved == 1
    assert len(la_sends(transport)) == 1  # start only; no end without a token
    row = find_activity_row(
        conn, apns_token=device.apns_token, card_key=card_key, track_id="trk1",
    )
    assert row["ended_at"] is None and row["stage"] == "pending_end"

    ended = await end_activity_if_card_closed(
        conn, device, transport, card_key=card_key, track_id="trk1",
        token="lateToken", now=6.0,
    )
    assert ended is True
    end = la_sends(transport)[-1]
    assert end["event"] == "end"
    assert end["token"] == "lateToken"
    row = find_activity_row(
        conn, apns_token=device.apns_token, card_key=card_key, track_id="trk1",
    )
    assert row["ended_at"] is not None
