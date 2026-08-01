from __future__ import annotations

from pathlib import Path

from frigate_sidecar import db
from frigate_sidecar.push import store
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.models import ReviewEvent
from frigate_sidecar.push.transport import LogTransport, TransportResult


def _make_engine(db_path: Path, transport=None) -> PushEngine:
    return PushEngine(
        db_path=str(db_path), transport=transport or LogTransport(), server_id="s1",
        handle_ttl_s=3600,
    )


def _register(db_path: Path, apns_token: str, **kwargs) -> None:
    conn = db.open_sidecar(db_path)
    store.upsert_device(conn, apns_token=apns_token, bundle_id="com.x", environment="sandbox",
                         **kwargs)
    conn.commit()
    conn.close()


def test_handle_event_notifies_matching_device_and_mints_handle(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["doorbell"])
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert", labels=("person",))
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    assert sent == 1
    assert len(transport.sent) == 1
    handle = transport.sent[0]["handle"]

    conn = db.open_sidecar(db_path)
    data = store.redeem_handle(conn, handle)
    conn.close()
    assert data == {"camera": "doorbell", "event_id": "r1"}


def test_handle_event_no_match_sends_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["garden"])
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    assert sent == 0
    assert transport.sent == []


def test_handle_review_payload_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1")
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


class _UnregisteringTransport:
    async def send(self, device, *, handle, server_id, severity, collapse_id):
        return TransportResult(ok=False, unregistered=True, error="410 Unregistered")


def test_410_prunes_device(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1")
    engine = _make_engine(db_path, _UnregisteringTransport())

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    assert sent == 0

    conn = db.open_sidecar(db_path)
    remaining = store.list_devices(conn)
    conn.close()
    assert remaining == []


class _FailingTransport:
    async def send(self, device, *, handle, server_id, severity, collapse_id):
        return TransportResult(ok=False, unregistered=False, error="503")


def test_transient_failure_does_not_prune(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1")
    engine = _make_engine(db_path, _FailingTransport())

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    import asyncio

    asyncio.run(engine.handle_event(event))

    conn = db.open_sidecar(db_path)
    remaining = store.list_devices(conn)
    conn.close()
    assert len(remaining) == 1
