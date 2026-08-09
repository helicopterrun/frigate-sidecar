"""Phase 5 WU7 — additional tests covering gaps not in test_push_phase5.py.

Organized by the 9 areas from the Phase 5 spec:
1. Per-device filtering (cameras, min_severity, two-device integration)
2. Sounding rate cap (silent pushes don't spend budget, window sliding)
3. Urgent re-sound (wire shape, stops on resolve, counts against rate cap)
4. Quiet hours integration (cap_quiet, mute_sounds, urgent exempt)
5. Payload contract (LA relevance-score, stale-date, dismissal-date)
6. Delivery hints (apns_priority/expiration on relay wire)
7. Relay key (x-relay-key header present/absent)
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from frigate_sidecar import db
from frigate_sidecar.config import PushSection
from frigate_sidecar.push import card_store, policy_settings, store
from frigate_sidecar.push.cards import Card, urgent_resound_due
from frigate_sidecar.push.delivery import (
    _device_eligible,
    apply_urgent_resound,
    build_card_payload,
    sound_name_for_card,
)
from frigate_sidecar.push.delivery_wire import (
    handle_delivery_event,
    handle_delivery_resolve,
)
from frigate_sidecar.push.live_activities import (
    build_content_state,
    build_la_end_payload,
    build_la_start_payload,
    build_la_update_payload,
)
from frigate_sidecar.push.models import Device, ReviewEvent
from frigate_sidecar.push.transport import LogTransport, RelayTransport


def _device(
    token: str = "tok1",
    *,
    cameras: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
    min_severity: str = "detection",
    push_to_start: str = "pts1",
) -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", cameras=cameras, labels=labels,
        min_severity=min_severity, push_to_start_token=push_to_start,
    )


def _event(
    camera: str = "doorbell",
    track_id: str = "trk1",
    label: str = "person",
    zones: tuple[str, ...] = ("pool",),
) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=(label,), track_ids=(track_id,), zones=zones,
    )


def _sit_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if "payload" in r and not r.get("live_activity")]


def _la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


# ── 1. Per-device filtering ─────────────────────────────────────────────

class TestDeviceEligibleExtended:
    def test_cameras_filter_excludes_non_matching(self):
        dev = _device(cameras=("patio",))
        assert not _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_cameras_filter_includes_matching(self):
        dev = _device(cameras=("doorbell", "patio"))
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_min_severity_alert_rejects_quiet(self):
        dev = _device(min_severity="alert")
        assert not _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="quiet",
        )

    def test_min_severity_alert_accepts_notify(self):
        dev = _device(min_severity="alert")
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_min_severity_detection_accepts_quiet(self):
        dev = _device(min_severity="detection")
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="quiet",
        )


@pytest.mark.asyncio
async def test_two_devices_only_eligible_one_receives(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    dev_match = _device("tok_match", cameras=("doorbell",))
    dev_miss = _device("tok_miss", cameras=("patio",))
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[dev_match, dev_miss], transport=transport,
        config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["device_id"] == "d_tok_match"


# ── 2. Sounding rate cap extended ────────────────────────────────────────

@pytest.mark.asyncio
async def test_silent_push_does_not_spend_rate_budget(sidecar_db_path: Path):
    """A quiet-level push (no sound) should not count toward the 10/hr cap."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        _event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    count = store.count_sends_since(
        conn, apns_token="tok1", situation_id="_card_sound", since=0.0,
    )
    assert count == 0


@pytest.mark.asyncio
async def test_rate_cap_window_slides(sidecar_db_path: Path):
    """Sends older than 1 hour don't count — the 11th push sounds if old ones aged out."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    for i in range(10):
        store.record_send(
            conn, apns_token="tok1", situation_id="_card_sound", now=50.0 + i,
        )
    conn.commit()

    # now=5000 → all 10 sends are >3600s old, window is clear
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5000.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert "sound" in sends[0]["payload"]["aps"]


# ── 3. Urgent re-sound ──────────────────────────────────────────────────

def test_urgent_resound_due_stops_at_max():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=5,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True, max_resounds=5)


def test_urgent_resound_due_fires_under_max():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=4,
    )
    assert urgent_resound_due(card, now=999, interval_s=120, enabled=True, max_resounds=5)


def test_urgent_resound_not_due_when_resolved():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=0, resolved=True,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


def test_urgent_resound_not_due_when_handled():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=0, handled=True,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


def test_resound_payload_is_escalate_with_sound():
    """A re-sound on the wire looks like an escalate with aps.sound set."""
    card = Card(
        card_key="doorbell:stranger:trk1", level="urgent",
        created_at=0, updated_at=0, state_since_at=0,
        last_sound_at=0, resound_count=1, peak_level="urgent",
    )
    card = apply_urgent_resound(card, now=500.0)
    payload = build_card_payload(
        card, "escalate", sound=True, subject_kind="stranger", place_class="off_limits",
        camera="doorbell", zone_name="pool", glyph="person.stranger",
        primary="Person at Pool", secondary="Pool · 500s", event_ts=500.0,
    )
    assert payload["mutation"] == "escalate"
    assert payload["aps"]["sound"] == "urgent.caf"
    assert payload["aps"]["interruption-level"] == "time-sensitive"


@pytest.mark.asyncio
async def test_resound_stops_on_resolve(sidecar_db_path: Path):
    """Once a card is resolved, urgent_resound_due returns False."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    # Create an urgent card
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # Resolve it
    await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="stranger", now=500.0,
    )
    card = card_store.get_card(conn, "doorbell:stranger:trk1")
    assert card is not None
    assert card.resolved
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


# ── 4. Quiet hours integration ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiet_hours_cap_quiet_caps_notify_to_quiet(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "cap_quiet"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "quiet"
    assert "sound" not in sends[0]["payload"]["aps"]


@pytest.mark.asyncio
async def test_quiet_hours_cap_quiet_exempts_urgent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "cap_quiet"}
    policy_settings.apply_settings(settings)

    # stranger + off_limits = urgent
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "urgent"


@pytest.mark.asyncio
async def test_quiet_hours_mute_sounds_strips_sound(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "mute_sounds"}
    policy_settings.apply_settings(settings)

    # stranger + doors = notify (would normally sound)
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "notify"
    assert "sound" not in sends[0]["payload"]["aps"]


@pytest.mark.asyncio
async def test_quiet_hours_mute_sounds_exempts_urgent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "mute_sounds"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert "sound" in sends[0]["payload"]["aps"]


def test_quiet_hours_boundary_start_is_inclusive():
    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "09:00", "end": "17:00", "mode": "cap_quiet"}
    active, _ = policy_settings.is_quiet_hours(settings, 540)  # exactly 09:00
    assert active is True


def test_quiet_hours_boundary_end_is_exclusive():
    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "09:00", "end": "17:00", "mode": "cap_quiet"}
    active, _ = policy_settings.is_quiet_hours(settings, 1020)  # exactly 17:00
    assert active is False


# ── 5. Payload contract — LA payloads ────────────────────────────────────

class TestLaPayloadContract:
    def test_la_start_relevance_score_by_level(self):
        for level, expected in [("urgent", 1.0), ("notify", 0.75), ("quiet", 0.5), ("log", 0.25)]:
            state = build_content_state(
                level=level, mutation="create", glyph="g", primary="P",
                secondary="S", elapsed_seconds=0, card_key="k",
                thumbnail_handle=None, thumbnail_revision=1,
            )
            payload = build_la_start_payload(
                content_state=state, family="person", camera="c",
                track_id="t", card_key="k", now=1000.0,
            )
            assert payload["aps"]["relevance-score"] == expected, f"level={level}"

    def test_la_start_stale_date_is_now_plus_900(self):
        state = build_content_state(
            level="notify", mutation="create", glyph="g", primary="P",
            secondary="S", elapsed_seconds=0, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_start_payload(
            content_state=state, family="person", camera="c",
            track_id="t", card_key="k", now=1000.0, stale_s=900.0,
        )
        assert payload["aps"]["stale-date"] == 1900

    def test_la_update_stale_date(self):
        state = build_content_state(
            level="notify", mutation="enrich", glyph="g", primary="P",
            secondary="S", elapsed_seconds=10, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(
            content_state=state, now=2000.0, stale_s=900.0,
        )
        assert payload["aps"]["stale-date"] == 2900

    def test_la_end_dismissal_date_plus_30s(self):
        state = build_content_state(
            level="notify", mutation="resolve", glyph="g", primary="P",
            secondary="S", elapsed_seconds=60, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_end_payload(content_state=state, now=3000.0, dismissal_offset=30.0)
        assert payload["aps"]["dismissal-date"] == 3030

    def test_la_update_escalation_alert_has_sound_and_interruption(self):
        state = build_content_state(
            level="urgent", mutation="escalate", glyph="g", primary="P",
            secondary="S", elapsed_seconds=30, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(
            content_state=state, now=1000.0, stale_s=900.0,
            alert=True, alert_title="P", alert_body="S",
            sound="urgent.caf", interruption_level="time-sensitive",
        )
        assert payload["aps"]["alert"] == {"title": "P", "body": "S"}
        assert payload["aps"]["sound"] == "urgent.caf"
        assert payload["aps"]["interruption-level"] == "time-sensitive"

    def test_la_update_no_alert_has_no_sound(self):
        state = build_content_state(
            level="notify", mutation="enrich", glyph="g", primary="P",
            secondary="S", elapsed_seconds=10, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(content_state=state, now=1000.0)
        assert "alert" not in payload["aps"]
        assert "sound" not in payload["aps"]


def test_sound_name_known_person_at_door():
    assert sound_name_for_card("notify", "known", "person") == "at-the-door.caf"


# ── 6. Delivery hints — apns_priority/expiration on relay wire ───────────

@pytest.mark.asyncio
async def test_relay_situation_carries_priority_and_expiration():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_situation(
        dev, payload={"aps": {}}, collapse_id="c1",
        apns_priority=10, apns_expiration=99999,
    )
    assert captured["json"]["apns-priority"] == 10
    assert captured["json"]["apns-expiration"] == 99999
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_situation_omits_priority_when_none():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert "apns-priority" not in captured["json"]
    assert "apns-expiration" not in captured["json"]
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_la_carries_priority_and_expiration():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_live_activity(
        dev, token="tok", payload={"aps": {}}, collapse_id="c1",
        event="update", apns_priority=5, apns_expiration=88888,
    )
    assert captured["json"]["apns-priority"] == 5
    assert captured["json"]["apns-expiration"] == 88888
    await relay.aclose()


# ── 7. Relay key on the wire ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relay_key_header_present_when_set():
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="secret123")
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert captured_headers["x-relay-key"] == "secret123"
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_key_header_absent_when_empty():
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="")
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert "x-relay-key" not in captured_headers
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_key_on_la_and_test_endpoints():
    """Every relay method (send, send_situation, send_live_activity, send_test)
    carries the x-relay-key header."""
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="k1")
    dev = _device()
    await relay.send(
        dev, handle="h", server_id="s", severity="alert", collapse_id="c",
    )
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c")
    await relay.send_live_activity(
        dev, token="t", payload={"aps": {}}, collapse_id="c", event="start",
    )
    await relay.send_test(dev)
    assert all(h.get("x-relay-key") == "k1" for h in seen_headers)
    assert len(seen_headers) == 4
    await relay.aclose()
