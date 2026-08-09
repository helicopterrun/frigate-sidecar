from __future__ import annotations

from pathlib import Path

from frigate_sidecar import db
from frigate_sidecar.push import store
from frigate_sidecar.config import PushSection
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import ReviewEvent
from frigate_sidecar.push.transport import LogTransport, TransportResult


def _make_engine(db_path: Path, transport=None) -> PushEngine:
    engine = PushEngine(
        db_path=str(db_path), transport=transport or LogTransport(), server_id="s1",
        handle_ttl_s=3600,
    )
    engine.push_config = PushSection(delivery_enabled=True)
    return engine


def _register(db_path: Path, apns_token: str, **kwargs) -> None:
    conn = db.open_sidecar(db_path)
    store.upsert_device(conn, apns_token=apns_token, bundle_id="com.x", environment="sandbox",
                         **kwargs)
    conn.commit()
    conn.close()


def test_handle_event_card_pipeline_creates_card(tmp_path: Path) -> None:
    """With the card pipeline (Phase 5), a person event at a door zone creates
    a card and sends via send_situation."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["doorbell"], min_severity="detection")
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(
        review_id="r1", camera="doorbell", severity="alert", labels=("person",),
        zones=("front_door",),
    )
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    assert sent == 1
    assert len(transport.sent) >= 1
    payload = transport.sent[0]["payload"]
    assert payload["mutation"] == "create"


def test_handle_event_no_match_sends_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["garden"])
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    # Card is still mutated (card_key created), but no push sent to devices
    # whose camera filter doesn't match.
    assert transport.sent == []


def test_handle_review_payload_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", min_severity="detection")
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    payload = {
        "type": "new",
        "after": {
            "id": "r1", "camera": "doorbell", "severity": "alert",
            "data": {"objects": ["person"], "detections": ["ev-1"]},
        },
    }
    import asyncio

    sent = asyncio.run(engine.handle_review_payload(payload))
    assert sent == 1
