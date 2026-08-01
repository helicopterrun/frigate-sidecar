from __future__ import annotations

import httpx

from frigate_sidecar.push.models import Device
from frigate_sidecar.push.transport import LogTransport, RelayTransport


def _device(**kwargs):
    defaults = dict(
        apns_token="tok1", device_id="d_abc", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox",
    )
    defaults.update(kwargs)
    return Device(**defaults)


async def test_log_transport_records_send_and_succeeds():
    transport = LogTransport()
    result = await transport.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is True
    assert result.unregistered is False
    assert len(transport.sent) == 1
    record = transport.sent[0]
    assert record["handle"] == "h_1"
    assert record["server_id"] == "s1"
    assert record["severity"] == "alert"
    # No content-bearing field ever reaches the transport.
    assert "camera" not in record
    assert "label" not in record


async def test_relay_transport_posts_minimal_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(apns_token="tokXYZ"),
        handle="h_9k2m4p7q", server_id="a1b2c3", severity="alert", collapse_id="review-1",
    )
    assert result.ok is True
    assert captured["url"] == "https://relay.example.test/v1/relay/push"
    payload = captured["json"]
    assert set(payload.keys()) == {
        "device_token", "environment", "handle", "server_id", "severity", "apns-collapse-id",
    }
    assert payload["device_token"] == "tokXYZ"
    assert payload["environment"] == "sandbox"
    assert payload["handle"] == "h_9k2m4p7q"
    assert payload["server_id"] == "a1b2c3"
    assert payload["severity"] == "alert"
    assert payload["apns-collapse-id"] == "review-1"
    await relay.aclose()


async def test_relay_transport_410_marks_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is True
    await relay.aclose()


async def test_relay_transport_400_bad_token_marks_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"reason": "BadDeviceToken"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.unregistered is True


async def test_relay_transport_other_error_not_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is False


async def test_relay_transport_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is False
