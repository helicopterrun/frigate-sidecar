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
from typing import Any, Protocol

import httpx

from frigate_sidecar.push.models import Device

logger = logging.getLogger(__name__)


def _exc_error(exc: BaseException) -> str:
    """A `TransportResult.error` string that is never empty.

    `str(exc)` is blank for some httpx exceptions (e.g. certain
    `ConnectError`/`ReadTimeout` instances carry no message), which produced
    logs like `ok=False error=` with nothing to diagnose. Falling back to the
    exception's class name keeps `error` diagnostic even then.
    """
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@dataclass
class TransportResult:
    ok: bool
    # True if the relay/APNs reported the token as permanently dead (410
    # Unregistered or 400 BadDeviceToken, spec §5) -- the caller prunes the
    # device row immediately rather than retrying.
    unregistered: bool = False
    error: str | None = None
    status_code: int | None = None


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

    async def send_situation(
        self,
        device: Device,
        *,
        payload: dict[str, Any],
        collapse_id: str,
        apns_priority: int | None = None,
        apns_expiration: int | None = None,
    ) -> TransportResult:
        """One Interrupt-tier situation push (plan §8).

        Distinct from `send()` because the payload is built *here*, not at the
        relay: a situation's title is its user-authored name and its body
        names the label and dwell, none of which a fixed severity-keyed
        template can produce. Plan §8's "Relay boundary" paragraph settles
        what that means for the privacy line -- the relay forwards these bytes
        to APNs in flight without persisting, logging, or inspecting them,
        which is what "content-free **at rest**" has always meant and is how
        every push service works. The snapshot still never transits it.
        """
        ...

    async def send_live_activity(
        self,
        device: Device,
        *,
        token: str,
        payload: dict[str, Any],
        collapse_id: str,
        event: str,
        apns_priority: int | None = None,
        apns_expiration: int | None = None,
    ) -> TransportResult:
        """One Live Activity push: `start`, `update`, or `end` (Phase 2).

        Three things make this not a `send_situation` with a different body,
        which is why it is its own method rather than a flag:

        * **A different token.** `start` goes to the device's push-to-start
          token, `update`/`end` to the per-activity token iOS minted. Neither
          is `device.apns_token`, so the token is passed in.
        * **A different `apns-push-type`.** `liveactivity`, not `alert`.
        * **A different `apns-topic`.** `<bundle-id>.push-type.liveactivity`.
          Apple rejects a live-activity push sent to the plain app topic.
        """
        ...

    async def send_test(self, device: Device) -> TransportResult:
        """One fixed, self-contained test alert to a single device (spec §1).

        Deliberately not `send()` with a throwaway handle: the spec's test push
        carries **no** `handle` key and no `mutable-content`, because there is
        nothing for the NSE to redeem -- it passes the payload through
        unmodified and a tap just opens the app. Routing it through the normal
        send path would hand the relay a handle that resolves to nothing.

        Environment routing is *not* bypassed: the device row's sandbox/prod
        value picks the APNs endpoint exactly as a real send would, so a
        black-holed token fails here the same way it fails in production. That
        is the entire point of the button.
        """
        ...


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

    async def send_situation(
        self, device: Device, *, payload: dict[str, Any], collapse_id: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "payload": payload,
            "collapse_id": collapse_id,
            "situation_id": payload.get("situation_id", ""),
            "handle": payload.get("handle", ""),
        }
        self.sent.append(record)
        alert = payload.get("aps", {}).get("alert", {})
        logger.info(
            "push (mock transport): device=%s situation=%s collapse_id=%s title=%r body=%r "
            "handle=%s -- not delivered anywhere; set push.transport=relay for a real send",
            device.device_id, payload.get("situation_id"), collapse_id,
            alert.get("title"), alert.get("body"), payload.get("handle"),
        )
        return TransportResult(ok=True)

    async def send_live_activity(
        self, device: Device, *, token: str, payload: dict[str, Any],
        collapse_id: str, event: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "live_activity": True,
            "event": event,
            "token": token,
            "payload": payload,
            "collapse_id": collapse_id,
        }
        self.sent.append(record)
        state = payload.get("aps", {}).get("content-state", {})
        logger.info(
            "push (mock transport): LA %s device=%s collapse_id=%s stage=%s dwell=%s "
            "-- not delivered anywhere; set push.transport=relay for a real send",
            event, device.device_id, collapse_id,
            state.get("stage"), state.get("dwell_seconds"),
        )
        return TransportResult(ok=True)

    async def send_test(self, device: Device) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "test": True,
        }
        self.sent.append(record)
        # Loud, because on a mock transport `{"sent": true}` is a statement
        # about this process only -- nothing reaches a phone. Someone pressing
        # the app's button and seeing success needs this line to exist.
        logger.info(
            "push (mock transport): TEST push for device=%s environment=%s -- "
            "not delivered anywhere; set push.transport=relay for a real send",
            device.device_id, device.environment,
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

    #: The sidecar's own API, its DB CHECK constraint and spec §1 all spell the
    #: production environment `prod`; the relay's wire API spells it
    #: `production` and rejects anything else with 422. Translating here, at the
    #: one boundary between the two vocabularies, keeps `prod` the only spelling
    #: anywhere else in this codebase. Without it every push to a prod-registered
    #: device was rejected and no production device could ever be notified --
    #: invisible so far only because deployments run the mock transport.
    _RELAY_ENVIRONMENT = {"prod": "production", "sandbox": "sandbox"}

    #: How long an idle connection to the relay is kept for reuse. Handoff
    #: item 15: one long-lived connection per sidecar process, so the first
    #: push after a quiet hour doesn't pay TLS setup on the interrupt path.
    #: Cloudflare closes idle edge connections on its own schedule, so this is
    #: a ceiling on our side, not a guarantee -- and the relay -> APNs hop is
    #: explicitly not ours to keep warm (Workers don't guarantee outbound
    #: reuse across isolates).
    _KEEPALIVE_EXPIRY_S = 600.0

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        relay_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or self._build_client(timeout)
        self._owns_client = client is None
        self._relay_key = relay_key
        if not relay_key:
            logger.warning("push: relay_key not set — relay requests will be unauthenticated")

    @classmethod
    def _build_client(cls, timeout: float) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_keepalive_connections=4, max_connections=8,
            keepalive_expiry=cls._KEEPALIVE_EXPIRY_S,
        )
        try:
            return httpx.AsyncClient(timeout=timeout, limits=limits, http2=True)
        except ImportError:
            # `httpx[http2]` (the `h2` package) isn't installed. HTTP/1.1
            # keep-alive still reuses the connection, which is where nearly
            # all of the saving in handoff item 15 comes from -- multiplexing
            # buys a single-request-at-a-time sender very little.
            logger.info(
                "push: h2 not installed, relay connection is HTTP/1.1 keep-alive "
                "(install frigate-sidecar with the http2 extra for HTTP/2)"
            )
            return httpx.AsyncClient(timeout=timeout, limits=limits)

    @classmethod
    def _environment(cls, device: Device) -> str:
        return cls._RELAY_ENVIRONMENT.get(device.environment, device.environment)

    def _headers(self) -> dict[str, str]:
        if self._relay_key:
            return {"x-relay-key": self._relay_key}
        return {}

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
            "environment": self._environment(device),
            "handle": handle,
            "server_id": server_id,
            "severity": severity,
            "apns-collapse-id": collapse_id,
        }
        url = f"{self.base_url}/v1/relay/push"
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            return TransportResult(ok=False, error=_exc_error(exc))

        return self._result(resp)

    async def send_situation(
        self, device: Device, *, payload: dict[str, Any], collapse_id: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        """POST a fully-built situation payload to `/v1/relay/situation`."""
        body: dict[str, Any] = {
            "device_token": device.apns_token,
            "environment": self._environment(device),
            "apns-collapse-id": collapse_id,
            "payload": payload,
        }
        if apns_priority is not None:
            body["apns_priority"] = apns_priority
        if apns_expiration is not None:
            body["apns_expiration"] = apns_expiration
        url = f"{self.base_url}/v1/relay/situation"
        try:
            resp = await self._client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            return TransportResult(ok=False, error=_exc_error(exc))
        return self._result(resp)

    async def send_live_activity(
        self, device: Device, *, token: str, payload: dict[str, Any],
        collapse_id: str, event: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        """POST a Live Activity push to `/v1/relay/liveactivity`."""
        body: dict[str, Any] = {
            "device_token": token,
            "environment": self._environment(device),
            "apns-collapse-id": collapse_id,
            "event": event,
            "payload": payload,
        }
        # Underscored keys are the relay's wire contract (checkDeliveryHints);
        # hyphenated variants are silently ignored there.
        if apns_priority is not None:
            body["apns_priority"] = apns_priority
        if apns_expiration is not None:
            body["apns_expiration"] = apns_expiration
        url = f"{self.base_url}/v1/relay/liveactivity"
        try:
            resp = await self._client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            return TransportResult(ok=False, error=_exc_error(exc))
        return self._result(resp)

    async def send_test(self, device: Device) -> TransportResult:
        """POST the test push to the relay's `/v1/relay/test`.

        A separate endpoint rather than a flag on `/v1/relay/push`, because the
        two payloads differ in kind: the normal one is templated by severity and
        carries `handle` + `mutable-content` for the NSE, while this one is a
        fixed literal alert with neither. `/v1/relay/push` validates `handle` as
        required, so a test send cannot go through it at all.

        Implemented in elsinore-push-relay (`/v1/relay/test`); a relay that
        predates it returns 404 here and surfaces as `test_send_failed` --
        not a silent success.
        """
        payload = {
            "device_token": device.apns_token,
            "environment": self._environment(device),
        }
        url = f"{self.base_url}/v1/relay/test"
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            return TransportResult(ok=False, error=_exc_error(exc))
        return self._result(resp)

    @staticmethod
    def _result(resp: httpx.Response) -> TransportResult:
        if resp.status_code == 200:
            return TransportResult(ok=True)
        if resp.status_code in (410, 400):
            # 410 Unregistered / 400 BadDeviceToken (spec §5): permanent,
            # never retried -- the caller deletes the device row.
            logger.warning(
                "push: relay %s body: %s", resp.status_code, resp.text[:500],
            )
            return TransportResult(
                ok=False, unregistered=True, error=f"HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code in (422, 429):
            # 422: relay rejected the payload shape; 429: per-token rate
            # limit (60/rolling hour) — both mean the push never reached
            # Apple, which otherwise looks identical to a delivered-but-
            # throttled LA update. Make them loud.
            logger.warning(
                "push: relay %s (%s) body: %s",
                resp.status_code,
                "rate limited" if resp.status_code == 429 else "rejected payload",
                resp.text[:500],
            )
        return TransportResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
