"""Event-driven zone escalation (`delivery_wire.handle_zone_transition`).

Reviews go quiet on stationary objects, so a loiter that drifts into hotter
ground must re-route from the event stream. The end-to-end test here replays
a REAL captured walk (2026-08-14, the user loitering by the Tesla in the
`charger` zone at 0.92 confidence) that the review-only pipeline missed —
the review's zone list froze at `driveway` and no escalation ever fired.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection
from frigate_sidecar.push import policy_settings, store
from frigate_sidecar.push.delivery_wire import handle_delivery_event, handle_zone_transition
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.transport import LogTransport

FIXTURE = Path(__file__).parent / "fixtures" / "capture-charger-loiter.jsonl"


@pytest.fixture(autouse=True)
def _settings_with_charger_restricted():
    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["zone_classes"] = {"charger": "off_limits", "driveway": "yard"}
    policy_settings.apply_settings(settings)
    yield
    policy_settings.apply_settings(policy_settings.default_settings())


def make_device(token: str = "tok1") -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", push_to_start_token="pts1", min_severity="detection",
    )


def la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


def card_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if "payload" in r and not r.get("live_activity")]


def review(camera: str, track: str, zones: tuple[str, ...]) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"rv-{track}", camera=camera, severity="alert",
        labels=("person",), track_ids=(track,), zones=zones,
    )


@pytest.mark.asyncio
async def test_zone_transition_escalates_and_late_starts_la(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # Story exists at quiet: person in the driveway (yard).
    await handle_delivery_event(
        review("stairway-tight", "trk1", ("driveway",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert la_sends(transport) == []

    # Event stream: drifted into the charger (off_limits) — escalate.
    sent = await handle_zone_transition(
        "stairway-tight", "trk1", ("charger", "driveway"), label="person",
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    assert sent >= 1
    start = [s for s in la_sends(transport) if s["event"] == "start"][-1]
    assert start["payload"]["aps"]["attributes"]["family"] == "person_restricted"
    assert start["payload"]["aps"]["alert"]["sound"] == "urgent.caf"
    # la_first suppresses card pushes while the LA covers — the escalation's
    # only surface is the LA start alert above.
    esc_cards = [c for c in card_sends(transport) if c["payload"]["mutation"] == "escalate"]
    assert esc_cards == []


@pytest.mark.asyncio
async def test_zone_transition_never_creates_or_deescalates(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # No card yet: hook must not mint a story.
    sent = await handle_zone_transition(
        "stairway-tight", "ghost", ("charger",), label="person",
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert sent == 0
    assert transport.sent == []

    # Card at urgent already (created in charger): cooler ground is a no-op —
    # deescalation stays review-authoritative.
    await handle_delivery_event(
        review("stairway-tight", "trk2", ("charger",)),
        conn=conn, devices=[device], transport=transport, config=config, now=1.0,
    )
    before = len(transport.sent)
    sent = await handle_zone_transition(
        "stairway-tight", "trk2", ("driveway",), label="person",
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )
    assert sent == 0
    assert len(transport.sent) == before


@pytest.mark.asyncio
async def test_captured_charger_loiter_escalates(tmp_path, sidecar_db_path: Path):
    """The real walk, verbatim from the MQTT flight recorder: review freezes
    at driveway, events carry the charger drift. Before the hook this
    produced zero escalation; now it must produce an urgent
    person_restricted story."""
    from frigate_sidecar.push.engine import PushEngine

    conn = db.open_sidecar(sidecar_db_path)
    device = make_device()
    store.upsert_device(
        conn, apns_token=device.apns_token, bundle_id=device.bundle_id,
        environment=device.environment, cameras=[], min_severity="detection",
        push_to_start_token=device.push_to_start_token,
    )
    conn.commit()
    conn.close()

    transport = LogTransport()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="test")
    engine.push_config = PushSection(delivery_enabled=True)

    for line in FIXTURE.read_text().splitlines():
        row = json.loads(line)
        if row["topic"].endswith("reviews"):
            await engine.handle_review_payload(row["payload"])
        else:
            await engine.handle_object_payload(row["payload"])

    starts = [s for s in la_sends(transport) if s["event"] == "start"]
    restricted = [
        s for s in starts
        if s["payload"]["aps"]["attributes"]["family"] == "person_restricted"
    ]
    assert restricted, f"no person_restricted LA start; starts={[s['payload']['aps']['attributes'] for s in starts]}"
    # The urgent escalation's surface is the LA start alert with sound —
    # card pushes are suppressed while the LA covers (la_first).
    assert restricted[-1]["payload"]["aps"]["alert"]["sound"] == "urgent.caf"
    assert restricted[-1]["payload"]["aps"]["content-state"]["level"] == "urgent"
