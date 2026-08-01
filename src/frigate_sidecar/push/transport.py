"""Publisher transports.

`PushTransport` is the interface the engine (`engine.py`) sends through;
`LogTransport` and `RelayTransport` are the two implementations.

**Decision override from the spec (§4's open question), confirmed:** the
relay-visible alert text is fully generic ("New alert on your server"-style)
rather than naming the camera. The relay's only inputs are exactly the
spec's minimal set -- `{device_token, environment, handle, server_id,
severity}` plus the `apns-collapse-id` -- and it constructs the templated
`aps` text itself; camera/label detail is redeemed later by the NSE from the
handle, never sent here. This makes both transports below equally content-
-free by construction: there's no camera-name parameter to plumb through
even if a future transport wanted one.

No real APNs credentials exist yet (spec §4 -- the relay is the only sane
way to get Elsinore's `.p8` key off the sidecar). `LogTransport` is what
every dev/test environment runs against until a relay is actually deployed;
`RelayTransport` is the wire contract the relay should implement, exercised
here against a mock relay in tests, never against real Apple infrastructure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from frigate_sidecar.push.models import Device

logger = logging.getLogger(__name__)


@dataclass
class TransportResult:
    ok: bool
    # True if the relay/APNs reported the token as permanently dead (410
    # Unregistered or 400 BadDeviceToken, spec §5) -- the caller prunes the
    # device row immediately rather than retrying.
    unregistered: bool = False
    error: str | None = None


class PushTransport(Protocol):
    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult: ...


class LogTransport:
    """Mock transport: logs what would be sent, always succeeds.

    Lets the entire pipeline -- MQTT subscriber, decision engine,
    registration, handle minting -- be exercised end to end with no APNs
    credentials and no relay deployed. `sent` records every call for tests.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "handle": handle,
            "server_id": server_id,
            "severity": severity,
            "collapse_id": collapse_id,
        }
        self.sent.append(record)
        logger.info(
            "push (mock transport): device=%s environment=%s severity=%s handle=%s "
            "collapse_id=%s server_id=%s",
            device.device_id, device.environment, severity, handle, collapse_id, server_id,
        )
        return TransportResult(ok=True)


class RelayTransport:
    """Posts the minimal relay payload (spec §4) over HTTP.

    The relay holds the one real `.p8` key and signs the APNs provider JWT;
    it never receives a camera name, label, or anything content-bearing --
    only `{device_token, environment, handle, server_id, severity}` plus the
    collapse id, which it uses to fill in a fixed, severity-keyed template
    before forwarding to APNs.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult:
        payload = {
            "device_token": device.apns_token,
            "environment": device.environment,
            "handle": handle,
            "server_id": server_id,
            "severity": severity,
            "apns-collapse-id": collapse_id,
        }
        url = f"{self.base_url}/v1/relay/push"
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            return TransportResult(ok=False, error=str(exc))

        if resp.status_code == 200:
            return TransportResult(ok=True)
        if resp.status_code in (410, 400):
            # 410 Unregistered / 400 BadDeviceToken (spec §5): permanent,
            # never retried -- the caller deletes the device row.
            return TransportResult(ok=False, unregistered=True, error=f"HTTP {resp.status_code}")
        return TransportResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
