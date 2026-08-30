"""Device-scoped Live Activity aggregation tests (Elsinore Phase 4): one
activity per device, aggregating all open eligible "cards" (stories).

Reuses fixtures/helpers from `test_push_live_activities_wire.py` (same
directory) rather than redefining them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection
from frigate_sidecar.push import store
from frigate_sidecar.push.delivery_wire import handle_delivery_event, handle_delivery_resolve
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.transport import LogTransport
from tests.test_push_live_activities_wire import (
    _reset_ladder_table,  # noqa: F401  (autouse fixture)
    attach_token,
    find_activity_row,
    la_sends,
    make_device,
    make_event,
)

#: Mirrors `test_push_delivery_wire.EXTERNAL_BASE_URL` -- needed alongside a
#: real `PushEngine` to actually mint a `media_handle` (`_media_for` mints
#: nothing without both).
EXTERNAL_BASE_URL = "http://192.168.50.207:5001"


@pytest.mark.asyncio
async def test_two_stories_one_activity(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # Story A: person at front_door -> notify, LA-eligible.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # Story B: a different track, well outside the cross-camera dedup
    # window, joins as a second eligible story on the same device activity.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # still only ONE start, ever

    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    join_update = updates[0]
    assert join_update["payload"]["aps"].get("alert") is not None
    content_state = join_update["payload"]["aps"]["content-state"]
    assert content_state["extra_stories"] == 1
    assert "camera" in content_state

    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row is not None
    assert row["ended_at"] is None
    # Only one open row for this device -- device-scoped, not per-card.
    rows = conn.execute(
        "SELECT COUNT(*) FROM push_activities WHERE apns_token = ? AND ended_at IS NULL",
        (device.apns_token,),
    ).fetchone()[0]
    assert rows == 1


@pytest.mark.asyncio
async def test_primary_flips_on_escalation(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # A: notify.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # B: joins at notify too.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    # Escalate B: move to off_limits -> urgent. Primary must flip to B.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )

    last_update = [r for r in la_sends(transport) if r["event"] == "update"][-1]
    content_state = last_update["payload"]["aps"]["content-state"]
    assert content_state["deep_link_card_key"] == "doorbell:person:trkB"
    assert content_state["level"] == "urgent"


@pytest.mark.asyncio
async def test_end_only_after_both_close(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    # Resolve A -- B is still open and eligible, so no `end` yet.
    resolved = await handle_delivery_resolve(
        "doorbell", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=40.0,
    )
    assert resolved == 1
    assert all(r["event"] != "end" for r in la_sends(transport))
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["ended_at"] is None

    # Resolve B -- nothing eligible remains -> end, row closes.
    resolved = await handle_delivery_resolve(
        "doorbell", "trkB", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=60.0,
    )
    assert resolved == 1
    ends = [r for r in la_sends(transport) if r["event"] == "end"]
    assert len(ends) == 1
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_dismissal_quiet_period(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    store.dismiss_activity(conn, row["activity_id"], now=5.0)
    conn.commit()

    # New story joins while dismissed -- no restart.
    sent_before = len(la_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    assert len(la_sends(transport)) == sent_before

    # Escalate B (e.g. move to off_limits -> urgent) breaks through the
    # tombstone: it's cleared and a fresh start is sent.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    assert store.find_dismissed_activity(conn, apns_token=device.apns_token) is None
    # A's original start, plus this fresh restart -- two starts total.
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 2


@pytest.mark.asyncio
async def test_tombstone_cleared_when_all_close(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    store.dismiss_activity(conn, row["activity_id"], now=5.0)
    conn.commit()

    resolved = await handle_delivery_resolve(
        "doorbell", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=20.0,
    )
    assert resolved == 1
    # Clean slate: the last open story closing clears the tombstone.
    assert store.find_dismissed_activity(conn, apns_token=device.apns_token) is None

    # A brand-new story after that gets a fresh start (the original story
    # A's own start plus this one -- two starts total, never a restart of
    # the tombstoned activity).
    await handle_delivery_event(
        make_event("doorbell", "trkC", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 2


@pytest.mark.asyncio
async def test_escalate_keeps_create_thumbnail_handle(sidecar_db_path: Path):
    """Regression test: an ESCALATE mints no fresh media (`_MEDIA_MUTATIONS`
    is create/enrich only), so before the sticky `media_handle` fix the
    content-state omitted `thumbnail_handle` entirely on escalation --
    blanking the widget's thumbnail mid-story. It must now carry the SAME
    handle the CREATE minted.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)

    # create: front_door -> notify, LA-eligible, mints media.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    create_handle = starts[0]["payload"]["aps"]["content-state"]["thumbnail_handle"]
    assert create_handle
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # escalate: pool -> off_limits/urgent for a person. Mints no media.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=10.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    escalate_state = updates[0]["payload"]["aps"]["content-state"]
    assert escalate_state["level"] == "urgent"
    assert escalate_state["thumbnail_handle"] == create_handle


@pytest.mark.asyncio
async def test_aggregate_uses_non_triggering_primarys_own_handle(sidecar_db_path: Path):
    """Two open stories where the non-triggering story is the aggregate
    PRIMARY: the emitted content-state's `thumbnail_handle` must be the
    PRIMARY's own persisted handle, not the triggering card's handle."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)

    # Story A: created at urgent (off_limits/person via "pool" zone) -- it
    # will stay the PRIMARY (highest level) through the rest of this test.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    a_handle = starts[0]["payload"]["aps"]["content-state"]["thumbnail_handle"]
    assert a_handle
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # Story B: a different, lower-level (notify) story joins and triggers
    # this mutation -- it is NOT the primary (A outranks it at urgent).
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=20.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    content_state = updates[0]["payload"]["aps"]["content-state"]
    # A is still primary (urgent beats notify).
    assert content_state["deep_link_card_key"] == "doorbell:person:trkA"
    assert content_state["thumbnail_handle"] == a_handle


@pytest.mark.asyncio
async def test_card_with_no_media_ever_minted_omits_thumbnail_handle(sidecar_db_path: Path):
    """A card that never had any media minted (no engine/external_base_url)
    keeps omitting `thumbnail_handle` from the content-state entirely --
    not `""`, not `None` in the dict, genuinely absent, matching today's
    behavior."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)  # no external_base_url, no engine

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    assert "thumbnail_handle" not in starts[0]["payload"]["aps"]["content-state"]

    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    assert "thumbnail_handle" not in updates[0]["payload"]["aps"]["content-state"]


def card_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if not r.get("live_activity") and not r.get("test")]


@pytest.mark.asyncio
async def test_la_first_demotion_covers_all_stories(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    def _assert_all_passive(sends: list[dict]) -> None:
        assert sends
        for s in sends:
            aps = s["payload"]["aps"]
            assert aps["interruption-level"] == "passive"
            assert not aps.get("sound")
            assert aps["mutable-content"] == 1

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # LA start accepted -- the card still delivers (NSE must still run) but
    # demoted to passive.
    _assert_all_passive(card_sends(transport))
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    before = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    # B is a new story joining the already-live device activity -- the
    # ordinary card push for B must be demoted too, not just A's.
    new_sends = card_sends(transport)[before:]
    _assert_all_passive(new_sends)

    # A further mutation of B (escalation) is still covered/demoted.
    before = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    _assert_all_passive(card_sends(transport)[before:])
