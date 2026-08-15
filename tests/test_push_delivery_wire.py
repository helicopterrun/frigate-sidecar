"""Wire-up tests for `push/delivery_wire.py`.

Covers the zone-identity fix (a tracked object that gains a zone mid-
lifetime must mutate its existing card, not fork a new one) and
cross-camera dedup (docs/push-notifications.md "Cross-camera
deduplication").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection
from frigate_sidecar.push.delivery_wire import handle_delivery_event, handle_delivery_resolve
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.transport import LogTransport

EXTERNAL_BASE_URL = "http://192.168.50.207:5001"


def make_device(token: str = "tok1", min_severity: str = "detection") -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity=min_severity,
    )


@pytest.mark.asyncio
async def test_zone_change_mutates_same_card_not_a_new_one(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    # external_base_url + engine are both present here on purpose: proves
    # `escalate` gets no `media` even when the infrastructure for it exists,
    # not merely because nothing was configured.
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)

    no_zone_event = ReviewEvent(
        review_id="r1", camera="alley-wide", severity="alert", labels=("car",),
        track_ids=("trk1",), zones=(),
    )
    zoned_event = ReviewEvent(
        review_id="r1", camera="alley-wide", severity="alert", labels=("car",),
        track_ids=("trk1",), zones=("parking_spot",),
    )

    # First evaluation: no zone yet -- thing/street caps at `log`, no push.
    await handle_delivery_event(
        no_zone_event, conn=conn, devices=[device], transport=transport,
        config=config, engine=engine, now=0.0,
    )
    rows = conn.execute("SELECT card_key, level FROM push_cards").fetchall()
    assert len(rows) == 1
    assert rows[0]["card_key"] == "alley-wide:vehicle:trk1"
    assert rows[0]["level"] == "log"
    assert transport.sent == []  # log never pushes

    # Second evaluation: same track id, now carries a zone -- vehicle/yard is
    # also `quiet` under v2. This must be the SAME card (enrich), not a
    # second one.
    await handle_delivery_event(
        zoned_event, conn=conn, devices=[device], transport=transport,
        config=config, engine=engine, now=10.0,
    )
    rows = conn.execute("SELECT card_key, level, zone_name FROM push_cards").fetchall()
    assert len(rows) == 1, "a zone change must mutate the existing card, not fork a new one"
    assert rows[0]["card_key"] == "alley-wide:vehicle:trk1"
    assert rows[0]["level"] == "log"
    assert rows[0]["zone_name"] == "parking_spot"

    # thing/yard = log (same as thing/street), so the zone change is an
    # enrich, not an escalate. log never pushes.
    assert len(transport.sent) == 0


@pytest.mark.asyncio
async def test_media_present_on_create_and_points_at_external_base(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)
    event = ReviewEvent(
        review_id="r1", camera="front", severity="alert", labels=("person",),
        track_ids=("trk1",), zones=("front_door",),  # doors -> notify: quiet no longer pushes (2026-08-14)
    )

    await handle_delivery_event(
        event, conn=conn, devices=[make_device()], transport=transport,
        config=config, engine=engine, now=0.0,
    )

    assert len(transport.sent) == 1
    payload = transport.sent[0]["payload"]
    assert payload["mutation"] == "create"
    media = payload.get("media")
    assert media is not None
    assert media.startswith(f"{EXTERNAL_BASE_URL}/v1/push/thumbnail/h_")
    assert "localhost" not in media
    assert "127.0.0.1" not in media

    # The handle in the URL is real, minted server-side against this event.
    handle = media.rsplit("/", 1)[-1]
    row = conn.execute(
        "SELECT camera, event_id FROM push_handles WHERE handle = ?", (handle,)
    ).fetchone()
    assert row is not None
    assert row["camera"] == "front"
    assert row["event_id"] == event.event_id


@pytest.mark.asyncio
async def test_media_present_on_enrich_too(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)
    event = ReviewEvent(
        review_id="r1", camera="front", severity="alert", labels=("person",),
        track_ids=("trk1",), zones=("front_door",),
    )

    await handle_delivery_event(
        event, conn=conn, devices=[make_device()], transport=transport,
        config=config, engine=engine, now=0.0,
    )
    await handle_delivery_event(
        event, conn=conn, devices=[make_device()], transport=transport,
        config=config, engine=engine, now=1.0,
    )

    assert len(transport.sent) == 2
    enrich_payload = transport.sent[1]["payload"]
    assert enrich_payload["mutation"] == "enrich"
    media = enrich_payload.get("media")
    assert media is not None
    assert media.startswith(f"{EXTERNAL_BASE_URL}/v1/push/thumbnail/h_")


@pytest.mark.asyncio
async def test_media_absent_without_external_base_url_even_with_engine(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True)  # external_base_url left at "" (default)
    event = ReviewEvent(
        review_id="r1", camera="front", severity="alert", labels=("person",),
        track_ids=("trk1",), zones=("front_door",),
    )

    await handle_delivery_event(
        event, conn=conn, devices=[make_device()], transport=transport,
        config=config, engine=engine, now=0.0,
    )

    assert len(transport.sent) == 1
    assert "media" not in transport.sent[0]["payload"]
    assert conn.execute("SELECT COUNT(*) FROM push_handles").fetchone()[0] == 0


def make_event(camera: str, track_id: str, zones: tuple[str, ...] = ()) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=("person",), track_ids=(track_id,), zones=zones,
    )


@pytest.mark.asyncio
async def test_two_cameras_same_zone_within_window_merge_into_one_card(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    rows = conn.execute("SELECT card_key, camera FROM push_cards").fetchall()
    assert len(rows) == 1, "same subject_kind + zone within the window must merge onto one card"
    assert rows[0]["card_key"] == "cam-a:person:trkA"
    assert rows[0]["camera"] == "cam-a", "merged card keeps the originating camera"

    assert len(transport.sent) == 2
    assert transport.sent[0]["payload"]["mutation"] == "create"
    merged_payload = transport.sent[1]["payload"]
    assert merged_payload["mutation"] == "enrich"
    assert merged_payload["card_key"] == "cam-a:person:trkA"
    assert merged_payload["camera"] == "cam-a"
    assert "also on Cam-B" in merged_payload["secondary"]

    alias = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE camera = 'cam-b' AND track_id = 'trkB'"
    ).fetchone()
    assert alias is not None
    assert alias["card_key"] == "cam-a:person:trkA"


@pytest.mark.asyncio
async def test_different_zones_do_not_merge(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("driveway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("back_yard",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert {r["card_key"] for r in rows} == {"cam-a:person:trkA", "cam-b:person:trkB"}


@pytest.mark.asyncio
async def test_same_zone_different_subject_kind_does_not_merge(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    person_event = make_event("cam-a", "trkA", zones=("driveway",))
    car_event = ReviewEvent(
        review_id="r2", camera="cam-b", severity="alert", labels=("car",),
        track_ids=("trkB",), zones=("driveway",),
    )

    await handle_delivery_event(
        person_event, conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        car_event, conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert {r["card_key"] for r in rows} == {"cam-a:person:trkA", "cam-b:vehicle:trkB"}


@pytest.mark.asyncio
async def test_no_zone_on_either_side_does_not_dedup(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA"), conn=conn, devices=[device], transport=transport,
        config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB"), conn=conn, devices=[device], transport=transport,
        config=config, now=5.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert {r["card_key"] for r in rows} == {"cam-a:person:trkA", "cam-b:person:trkB"}


@pytest.mark.asyncio
async def test_dedup_window_expired_creates_separate_card(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert {r["card_key"] for r in rows} == {"cam-a:person:trkA", "cam-b:person:trkB"}


@pytest.mark.asyncio
async def test_three_cameras_sharing_a_zone_all_merge_onto_the_first(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=4.0,
    )
    await handle_delivery_event(
        make_event("cam-c", "trkC", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=8.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert [r["card_key"] for r in rows] == ["cam-a:person:trkA"]


@pytest.mark.asyncio
async def test_subject_changing_zones_enriches_the_same_card_no_new_one(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    # Both zones guess to the same place class ("yard", Elsinore Phase 4's
    # name heuristic -- `policy_settings.guess_zone_class`), so the level is
    # unchanged and the mutation is `enrich`: this test is about the zone
    # move landing on the *same card*, not about a level change.
    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("driveway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("parking_spot",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    rows = conn.execute("SELECT card_key, zone_name FROM push_cards").fetchall()
    assert len(rows) == 1
    assert rows[0]["zone_name"] == "parking_spot"
    # person/yard routes quiet, and quiet no longer pushes (2026-08-14).
    assert transport.sent == []


@pytest.mark.asyncio
async def test_resolving_the_merged_secondary_track_leaves_the_primary_card_open(
    sidecar_db_path: Path,
):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    resolved = await handle_delivery_resolve(
        "cam-b", "trkB", conn=conn, devices=[device], transport=transport, config=config, now=6.0,
    )
    assert resolved == 0, "a merged contributor resolving must not resolve the shared card"

    card = conn.execute(
        "SELECT closed, resolved FROM push_cards WHERE card_key = 'cam-a:person:trkA'"
    ).fetchone()
    assert card["closed"] == 0
    assert card["resolved"] == 0

    alias = conn.execute(
        "SELECT 1 FROM push_card_track_aliases WHERE camera = 'cam-b' AND track_id = 'trkB'"
    ).fetchone()
    assert alias is None


@pytest.mark.asyncio
async def test_resolving_the_primary_track_resolves_the_card_normally(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("cam-a", "trkA", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("cam-b", "trkB", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )

    resolved = await handle_delivery_resolve(
        "cam-a", "trkA", conn=conn, devices=[device], transport=transport, config=config, now=6.0,
    )
    assert resolved == 1

    card = conn.execute(
        "SELECT closed, resolved FROM push_cards WHERE card_key = 'cam-a:person:trkA'"
    ).fetchone()
    assert card["closed"] == 1
    assert card["resolved"] == 1


@pytest.mark.asyncio
async def test_changing_the_routing_table_via_settings_changes_the_level_applied(
    sidecar_db_path: Path,
):
    """Elsinore Phase 4: `push/policy_settings.py`'s applied routing table
    is what `handle_delivery_event` actually evaluates cards against --
    `PUT /v1/push/settings` reaching a running card pipeline with no
    restart, exercised here one layer below the HTTP route
    (`tests/test_push_settings_routes.py` covers the route itself)."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    new_settings = policy_settings.default_settings()
    table_key = "routing_table_v2" if "routing_table_v2" in new_settings else "routing_table"
    new_settings[table_key]["thing"]["yard"] = "urgent"
    policy_settings.apply_settings(new_settings)

    await handle_delivery_event(
        ReviewEvent(
            review_id="r1", camera="cam-a", severity="alert", labels=("package",),
            track_ids=("trkA",), zones=("driveway",),
        ),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    row = conn.execute(
        "SELECT level FROM push_cards WHERE card_key = 'cam-a:thing:trkA'"
    ).fetchone()
    assert row["level"] == "urgent"


@pytest.mark.asyncio
async def test_zone_classes_from_settings_take_priority_over_the_guess_heuristic(
    sidecar_db_path: Path,
):
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    # "driveway" would normally guess as "yard"; force it to "doors" via
    # settings and confirm the card's place_class (and therefore level)
    # reflects the override, not the heuristic.
    new_settings = policy_settings.default_settings()
    new_settings["zone_classes"]["driveway"] = "doors"
    policy_settings.apply_settings(new_settings)

    await handle_delivery_event(
        ReviewEvent(
            review_id="r1", camera="cam-a", severity="alert", labels=("person",),
            track_ids=("trkA",), zones=("driveway",),
        ),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sent = transport.sent[0]["payload"]
    assert sent["place_class"] == "doors"
    assert sent["level"] == "notify"  # person/doors per the default table


@pytest.mark.asyncio
async def test_zone_override_via_settings_is_applied_to_a_real_card(sidecar_db_path: Path):
    """Elsinore Phase 4 addendum: a `zone_overrides[zone][subject]` entry
    reaches a real card evaluation one layer below the HTTP route
    (`tests/test_push_settings_routes.py` covers the route itself)."""
    from frigate_sidecar.push import policy_settings

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    # Base table says "thing"/"doors" is "log" (never pushed); the override
    # forces "notify" for this specific zone regardless.
    new_settings = policy_settings.default_settings()
    new_settings["zone_classes"]["front_entry_person"] = "doors"
    new_settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    policy_settings.apply_settings(new_settings)
    assert new_settings["routing_table"]["thing"]["doors"] == "log"

    await handle_delivery_event(
        ReviewEvent(
            review_id="r1", camera="cam-a", severity="alert", labels=("package",),
            track_ids=("trkA",), zones=("front_entry_person",),
        ),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    row = conn.execute(
        "SELECT level FROM push_cards WHERE card_key = 'cam-a:thing:trkA'"
    ).fetchone()
    assert row["level"] == "notify"
    assert transport.sent[0]["payload"]["level"] == "notify"


@pytest.mark.asyncio
async def test_delivery_disabled_is_a_no_op(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=False)
    event = ReviewEvent(
        review_id="r1", camera="front", severity="alert", labels=("person",),
        track_ids=("trk1",),
    )
    mutated = await handle_delivery_event(
        event, conn=conn, devices=[make_device()], transport=transport, config=config, now=0.0,
    )
    assert mutated == 0
    assert conn.execute("SELECT COUNT(*) FROM push_cards").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_neighbor_cameras_merge_despite_disjoint_zones(sidecar_db_path: Path):
    """The 2026-08-14 20:55 walk: stairway-tight (no shared zones) and
    walkway saw the same person 10s apart and produced two stories. With
    the cameras declared neighbors, the second track merges onto the first
    card even though the zone sets are disjoint."""
    from frigate_sidecar.push import policy_settings

    settings = policy_settings.default_settings()
    settings["camera_neighbors"] = {"stairway-tight": ["walkway"]}
    policy_settings.apply_settings(settings)

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("stairway-tight", "trkA", zones=("stairs",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # Walkway lists a completely different zone -- old dedup missed this.
    await handle_delivery_event(
        make_event("walkway", "trkB", zones=("walkway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert [r["card_key"] for r in rows] == ["stairway-tight:person:trkA"]
    alias = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE camera = 'walkway'"
    ).fetchone()
    assert alias is not None and alias["card_key"] == "stairway-tight:person:trkA"


@pytest.mark.asyncio
async def test_neighbor_declaration_is_symmetric(sidecar_db_path: Path):
    from frigate_sidecar.push import policy_settings

    settings = policy_settings.default_settings()
    settings["camera_neighbors"] = {"stairway-tight": ["walkway"]}
    policy_settings.apply_settings(settings)

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    # First sighting on the *declared* side; second on the undeclared side.
    await handle_delivery_event(
        make_event("walkway", "trkA", zones=("walkway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    await handle_delivery_event(
        make_event("stairway-tight", "trkB", zones=("stairs",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert [r["card_key"] for r in rows] == ["walkway:person:trkA"]


@pytest.mark.asyncio
async def test_non_neighbor_cameras_with_disjoint_zones_still_split(sidecar_db_path: Path):
    from frigate_sidecar.push import policy_settings

    settings = policy_settings.default_settings()
    settings["camera_neighbors"] = {"stairway-tight": ["walkway"]}
    policy_settings.apply_settings(settings)

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    device = make_device()

    await handle_delivery_event(
        make_event("stairway-tight", "trkA", zones=("stairs",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # "garden" is not declared a neighbor of stairway-tight.
    await handle_delivery_event(
        make_event("garden", "trkB", zones=("front_garden",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards ORDER BY created_at").fetchall()
    assert {r["card_key"] for r in rows} == {
        "stairway-tight:person:trkA", "garden:person:trkB",
    }
