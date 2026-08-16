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


@pytest.fixture(autouse=True)
def _reset_ladder_table():
    """Several tests below install a custom routing table via
    `ladder_policy.set_table` — restore the default afterwards so table
    state never leaks into later tests (it did: the routing-gated family
    change surfaced a create-at-quiet leak that failed an unrelated test)."""
    from frigate_sidecar.push import ladder_policy
    yield
    ladder_policy.set_table({k: dict(v) for k, v in ladder_policy.TABLE.items()})


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
async def test_card_push_suppressed_while_la_confirmed(sidecar_db_path: Path):
    """la_first: once the LA demonstrably exists, the story sends NO card
    pushes to that device at all — a passive row updating alongside the LA
    was duplicate noise (2026-08-14). The resolve push (elsewhere) is the
    one durable Notification Center record."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:person:trk1"

    # create: person at front_door -> notify; LA start accepted by the mock
    # transport, so the create card push is suppressed entirely.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport)[0]["event"] == "start"
    assert card_sends(transport) == []

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perActivity1")

    # escalate: person moves to pool (off_limits -> urgent). LA update lands
    # on the confirmed token and carries the escalation alert; still no card.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    last_la = la_sends(transport)[-1]
    assert last_la["event"] == "update"
    assert last_la["payload"]["aps"].get("alert") is not None
    assert card_sends(transport) == []


@pytest.mark.asyncio
async def test_card_push_not_demoted_when_la_unconfirmed(sidecar_db_path: Path):
    """The failure mode that forced the eaac866 revert: an LA that never
    materializes must not eat the banner. A device with no push-to-start
    token gets a full-fat card push; so does an escalate whose LA row has
    no per-activity token yet."""
    from frigate_sidecar.push import policy_settings
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

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
    # And the token-less row was kept alive for the sweeper.
    row = find_activity_row(
        conn, apns_token=device.apns_token, card_key="porch:person:trkB", track_id="trkB",
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
    assert "sound" not in start["payload"]["aps"].get("alert", {})


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


@pytest.mark.asyncio
async def test_la_only_mode_no_banner_ever_and_catch_all_family(sidecar_db_path: Path):
    """live_activities.la_only: every pushable card gets an activity (the
    catch-all family covers events outside the curated four) and every card
    push is passive/silent — no banner, no sound, even before the LA is
    confirmed and even on escalation."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["live_activities"]["la_only"] = True
    policy_settings.apply_settings(settings)

    # An animal in the yard matches no curated family — catch-all covers it.
    await handle_delivery_event(
        make_event("driveway", "trk9", "dog", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    start = la_sends(transport)[0]
    assert start["event"] == "start"
    assert start["payload"]["aps"]["attributes"]["family"] == "activity"
    assert start["payload"]["aps"]["content-state"]["glyph"] == "pawprint.fill"
    assert "sound" not in start["payload"]["aps"]  # fully silent start
    # quiet never card-pushes (2026-08-14), even in la_only -- the LA is
    # the whole surface for a quiet story.
    assert card_sends(transport) == []

    # Escalation: LA update stays silent (no alert dict), card push passive.
    attach_token(conn, device=device, card_key="driveway:animal:trk9",
                 track_id="trk9", token="tokLA")
    await handle_delivery_event(
        make_event("driveway", "trk9", "dog", zones=("charger",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    for send in la_sends(transport):
        if send["event"] == "update":
            assert "alert" not in send["payload"]["aps"]
    for send in card_sends(transport):
        assert send["payload"]["aps"]["interruption-level"] == "passive"
        assert "sound" not in send["payload"]["aps"]


@pytest.mark.asyncio
async def test_la_only_mode_skips_log_level_cards(sidecar_db_path: Path):
    """Catch-all must not mint activities for log-level noise."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["live_activities"]["la_only"] = True
    policy_settings.apply_settings(settings)

    # thing on street = log level -> no push, no activity
    await handle_delivery_event(
        make_event("alley-cam", "trk1", "car", zones=()),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []


@pytest.mark.asyncio
async def test_la_only_ineligible_family_falls_back_to_catch_all(sidecar_db_path: Path):
    """2026-08-12: with la_only on, a curated family that's toggled off must
    not leave the device with neither surface -- the card push is always
    passive/silent in la_only mode, so a skipped LA meant nothing alerted at
    all. The card must ride the catch-all activity instead of nothing."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:thing:trk1"

    settings = policy_settings.default_settings()
    settings["live_activities"]["la_only"] = True
    settings["live_activities"]["package"] = False
    policy_settings.apply_settings(settings)

    # create: package at pool zone -> thing/off_limits = quiet (pushable),
    # but the package family itself is disabled.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 1
    assert sends[0]["event"] == "start"
    assert sends[0]["payload"]["aps"]["attributes"]["family"] == "activity"
    # Catch-all glyph is by subject kind ("thing"), not the package glyph.
    assert sends[0]["payload"]["aps"]["content-state"]["glyph"] == "cube.fill"

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perActivity1")

    # enrich: no_row skip must not recur -- the same activity updates.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 2
    assert sends[1]["event"] == "update"
    assert sends[1]["token"] == "perActivity1"

    # resolve: the same (only) activity row ends.
    resolved = await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=30.0,
    )
    assert resolved == 1
    sends = la_sends(transport)
    assert len(sends) == 3
    assert sends[2]["event"] == "end"

    row = find_activity_row(conn, apns_token=device.apns_token, card_key=card_key, track_id="trk1")
    assert row is not None
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_la_only_off_disabled_family_skips_without_fallback(sidecar_db_path: Path):
    """The catch-all fallback is an `la_only`-only behavior. With `la_only`
    off, a disabled family must still just skip the LA -- and, since no LA
    covers it, the ordinary card push must alert normally, not be demoted.
    Uses person/doors (notify) rather than package/off_limits (quiet) so a
    demoted payload (passive, no sound) is visibly different from a normal
    one (active, sound present)."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["live_activities"]["person"] = False
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []
    cards = [r for r in transport.sent if "payload" in r and not r.get("live_activity")]
    assert len(cards) == 1
    # Not demoted: full alerting card (active + sound), since no LA covers
    # this device -- family-disabled must not silently downgrade the push.
    assert cards[0]["payload"]["aps"]["interruption-level"] == "active"
    assert cards[0]["payload"]["aps"].get("sound")


@pytest.mark.asyncio
async def test_la_only_eligible_family_uses_native_family_not_catch_all(sidecar_db_path: Path):
    """Regression guard: la_only must not blanket every card onto the
    catch-all -- an eligible curated family (person/doors) still gets its
    own family, keeping its glyph/copy."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["live_activities"]["la_only"] = True
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sends = la_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["aps"]["attributes"]["family"] == "person"


@pytest.mark.asyncio
async def test_multi_device_demotion_is_per_device(sidecar_db_path: Path):
    """Coverage is per-device: device A's confirmed LA demotes only A's card
    push; device B (no push-to-start token, so no LA) keeps the full
    alerting card. A single OR-ed coverage bool would silence B entirely."""
    from frigate_sidecar.push import policy_settings
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    dev_a = make_device(token="tokA")                      # LA-capable
    dev_b = make_device(token="tokB", push_to_start="")    # no LA
    config = PushSection(delivery_enabled=True)

    # person at front_door -> notify: A gets an LA start, B cannot.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[dev_a, dev_b], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert [r["device_id"] for r in starts] == ["d_tokA"]

    cards = {r["device_id"]: r for r in card_sends(transport)}
    # A: suppressed entirely — its LA start was APNs-accepted (la_first).
    # B: full alerting card — no LA covers it.
    assert set(cards) == {"d_tokB"}
    assert cards["d_tokB"]["payload"]["aps"]["interruption-level"] == "active"
    assert cards["d_tokB"]["payload"]["aps"].get("sound")


@pytest.mark.asyncio
async def test_urgent_escalation_late_starts_la_with_sound(sidecar_db_path: Path):
    """A person routed quiet at create qualifies for no LA (routing-gated
    families). When the story escalates to urgent, the LA *late-starts* —
    and its mandatory start alert carries the sound, doubling as the
    escalation alert. The card push is demoted; the LA is the surface."""
    from frigate_sidecar.push import ladder_policy, policy_settings

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)
    custom_table = {k: dict(v) for k, v in ladder_policy.TABLE.items()}
    custom_table["person"]["doors"] = "quiet"
    ladder_policy.set_table(custom_table)

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # Create at quiet (person at front_door, custom table): no LA — quiet
    # people don't mint activities.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []

    # Escalate to urgent (pool = off_limits): late start with the sound.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    start = [s for s in la_sends(transport) if s["event"] == "start"][-1]
    assert start["payload"]["aps"]["alert"]["sound"] == "urgent.caf"
    # Card push stays demoted — the LA carries the sound now.
    # The card push is suppressed entirely — the LA is the only surface;
    # only the pre-LA quiet create ever produced a card send.
    esc_cards = [c for c in card_sends(transport) if c["payload"]["mutation"] == "escalate"]
    assert esc_cards == []


@pytest.mark.asyncio
async def test_notify_escalation_la_update_alert_no_sound(sidecar_db_path: Path):
    """Notify-level alerting updates (haptic pop) must NOT carry a sound —
    only urgent escalation gets sound on the LA update. Verified via the
    person-at-doors create: the LA start (required by iOS) has the start
    sound, but any subsequent update at notify level carries no sound."""
    from frigate_sidecar.push import policy_settings
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:person:trk1"

    # Create at notify: LA start carries a sound (at-the-door.caf).
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    start = la_sends(transport)[0]
    assert start["event"] == "start"
    assert start["payload"]["aps"]["alert"].get("sound") == "at-the-door.caf"

    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perAct1")

    # Enrich at notify: LA update has no alert (enrich never alerts) and no sound.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )
    enrich_update = [s for s in la_sends(transport) if s["event"] == "update"][0]
    assert "alert" not in enrich_update["payload"]["aps"]
    assert "sound" not in enrich_update["payload"]["aps"]


@pytest.mark.asyncio
async def test_la_update_sound_suppressed_by_exhausted_budget(sidecar_db_path: Path):
    """The per-card sound budget (1) caps the LA update sound: after the
    first sounded escalation (quiet→urgent), a re-escalation gets an alert
    but no sound. Uses custom routing (person+doors=quiet) so the create
    doesn't spend the budget while still qualifying for the person LA."""
    from frigate_sidecar.push import ladder_policy, policy_settings

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)
    custom_table = {k: dict(v) for k, v in ladder_policy.TABLE.items()}
    custom_table["person"]["doors"] = "quiet"
    ladder_policy.set_table(custom_table)

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:person:trk1"

    # Create at quiet (person at front_door, custom table): LA starts, no sound.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    # Sound #1: escalate to urgent — the LA late-starts and its start alert
    # carries the sound (routing-gated families: quiet create minted no LA).
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    esc1 = [s for s in la_sends(transport) if s["event"] == "start"][-1]
    assert esc1["payload"]["aps"]["alert"]["sound"] == "urgent.caf"
    attach_token(conn, device=device, card_key=card_key, track_id="trk1", token="perAct1")

    # Deescalate back to notify (no sound on deescalate).
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    # Re-escalate to urgent: budget exhausted (sound_count=1), no sound.
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=30.0,
    )
    reesc = [s for s in la_sends(transport) if s["event"] == "update"][-1]
    assert reesc["payload"]["aps"].get("alert") is not None
    assert "sound" not in reesc["payload"]["aps"].get("alert", {})


@pytest.mark.asyncio
async def test_mute_sounds_strips_la_update_sound(sidecar_db_path: Path):
    """Global mute_sounds strips the sound key from urgent LA updates but
    still carries the alert dict (title/body) for haptic pop — muted is a
    sound-only control, not a suppression gate."""
    from frigate_sidecar.push import ladder_policy, policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    _card_key = "doorbell:person:trk1"

    # Custom table: person+doors=quiet so create doesn't spend sound budget.
    unmuted = policy_settings.default_settings()
    unmuted["mute_sounds"] = False
    policy_settings.apply_settings(unmuted)
    custom_table = {k: dict(v) for k, v in ladder_policy.TABLE.items()}
    custom_table["person"]["doors"] = "quiet"
    ladder_policy.set_table(custom_table)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []  # quiet create mints no LA

    # Enable mute_sounds, then send the escalation event — the LA
    # late-starts; its mandatory start alert pops but carries no sound.
    muted = policy_settings.default_settings()
    muted["mute_sounds"] = True
    policy_settings.apply_settings(muted)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    starts = [s for s in la_sends(transport) if s["event"] == "start"]
    assert len(starts) >= 1
    last = starts[-1]
    alert = last["payload"]["aps"].get("alert")
    assert alert is not None
    assert alert["title"]
    assert alert["body"]
    assert "sound" not in alert


@pytest.mark.asyncio
async def test_uncovered_urgent_card_keeps_card_push_sound(sidecar_db_path: Path):
    """When no LA covers the device (no push-to-start token), the card push
    retains its sound — the demotion only fires for LA-covered devices."""
    from frigate_sidecar.push import policy_settings
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []
    cards = card_sends(transport)
    assert len(cards) == 1
    assert cards[0]["payload"]["aps"]["sound"] == "urgent.caf"
    assert cards[0]["payload"]["aps"]["interruption-level"] == "time-sensitive"


@pytest.mark.asyncio
async def test_muted_urgent_escalation_la_carries_alert_without_sound(sidecar_db_path: Path):
    """Change 1: muted urgent escalation LA update carries alert dict
    (title/body) for haptic pop but no sound key."""
    from frigate_sidecar.push import ladder_policy, policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    _card_key = "doorbell:person:trk1"

    # Custom table: person+doors=quiet so create doesn't spend sound budget.
    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)
    custom_table = {k: dict(v) for k, v in ladder_policy.TABLE.items()}
    custom_table["person"]["doors"] = "quiet"
    ladder_policy.set_table(custom_table)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []  # quiet create mints no LA

    # Enable mute, then escalate to urgent — LA late-starts, alert muted.
    settings["mute_sounds"] = True
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    start = [s for s in la_sends(transport) if s["event"] == "start"][-1]
    alert = start["payload"]["aps"]["alert"]
    assert alert is not None
    assert alert["title"]
    assert alert["body"]
    assert "sound" not in alert


@pytest.mark.asyncio
async def test_muted_urgent_uncovered_card_keeps_time_sensitive_no_sound(sidecar_db_path: Path):
    """Change 1: muted urgent card push (no LA) keeps interruption-level
    time-sensitive but drops sound."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = True
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []
    cards = card_sends(transport)
    assert len(cards) == 1
    aps = cards[0]["payload"]["aps"]
    assert aps["interruption-level"] == "time-sensitive"
    assert "sound" not in aps


@pytest.mark.asyncio
async def test_muted_notify_stays_non_alerting(sidecar_db_path: Path):
    """Muted notify: card pushes without sound, no escalation to alerting."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = True
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    cards = card_sends(transport)
    assert len(cards) == 1
    aps = cards[0]["payload"]["aps"]
    assert aps["interruption-level"] == "active"
    assert "sound" not in aps
