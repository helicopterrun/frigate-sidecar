# Push notifications

Implements the design in Elsinore's `sidecar-push-notifications-spec.md`
(v0.3 draft). This doc is a short pointer at the sidecar's own code, not a
restatement of the full spec — see that doc for the complete rationale
(architecture, APNs key custody, NSE contract, testing plan).

## What's implemented

- **Event source:** `frigate_sidecar/push/mqtt.py` subscribes to
  `frigate/reviews` over MQTT (not `/api/events` polling, per the spec).
  `frigate/available` is also watched so a Frigate outage is distinguished
  from "no devices matched"; `offline_silence_s` of broker silence triggers
  the same back-fill path (`GET /api/events?after=...`) used on reconnect.
- **Decision engine:** `frigate_sidecar/push/decision.py` is a pure,
  dependency-free module — parses a `frigate/reviews` payload into a
  `ReviewEvent` and matches it against each registered device's
  `cameras`/`labels`/`min_severity` subscription filters. `[]` means "all" for
  both list filters, matching the registration contract.
- **Device registry:** `PUT`/`DELETE /v1/push/devices/{apns_token}`
  (`routes/push.py`) behind the sidecar's existing Frigate-session auth — no
  second credential. Registration is an idempotent PUT keyed on the token
  itself (`push/store.py`), so a relaunch that reuses the same token
  overwrites filter state rather than duplicating it.
- **Publisher, behind a transport interface** (`push/transport.py`):
  - `LogTransport` — logs what would be sent and always succeeds. This is
    the default (`push.transport: mock`) and what every test in this repo
    runs against, since no real APNs credentials exist yet.
  - `RelayTransport` — posts `{device_token, environment, handle, server_id,
    severity, apns-collapse-id}` to `push.relay_base_url`. The deployed
    relay implementing this contract is
    [elsinore-push-relay](https://github.com/helicopterrun/elsinore-push-relay)
    (a Cloudflare Worker holding the one team-bound APNs key), the default
    `relay_base_url`. To go live: `push.enabled: true`,
    `push.transport: relay`, and MQTT pointed at Frigate's broker. Tests
    still run against a mock relay, never real Apple infrastructure.

  **`prod` vs `production`.** This sidecar's `/v1/push/devices` API, its DB
  CHECK constraint and spec §1 all spell the production environment `prod`;
  the relay's wire API spells it `production` and rejects anything else with
  422. `RelayTransport` translates at that one boundary, so `prod` stays the
  only spelling everywhere else here. Before that, every push to a
  prod-registered device would have been rejected and no production device
  could ever have been notified — invisible only while the mock transport is
  in use.

  **Test push needs a second relay route.** `send_test` posts
  `{device_token, environment}` to `POST {relay_base_url}/v1/relay/test`:
  `/v1/relay/push` validates `handle` as required and templates its text by
  severity, so the test payload (fixed literal text, no `handle`, no
  `mutable-content`) cannot go through it. Added in
  [elsinore-push-relay#1](https://github.com/helicopterrun/elsinore-push-relay/pull/1)
  — until that is merged and deployed, a test send returns 404 from the relay
  and surfaces as `502 test_send_failed`, visibly broken rather than a silent
  success.
- **Test push:** `POST /v1/push/devices/{apns_token}/test` sends one fixed
  alert (`"Test notification"` / `"Push notifications are working."`,
  `sound: default`) to exactly that device, bypassing its camera/label/severity
  filters but **not** its environment routing — the point is to prove the APNs
  pipe, so a black-holed sandbox/prod mismatch must still fail here. `200
  {"sent": true}` means APNs accepted the request; there is no delivery
  receipt. `404` is reserved for "token not registered" (the released iOS
  client maps it to "your server doesn't support test notifications yet", so
  nothing else may borrow it); push switched off is `503 push_disabled` and a
  rejected send is `502 test_send_failed`. A `410`/`400` deletes the device row
  via the same §5 cleanup a real send applies.
- **Handle redemption:** `GET /v1/push/handle/{handle}` resolves a
  sidecar-minted, short-lived opaque handle to `{camera, event_id,
  snapshot_url}` for the iOS NSE to fetch a thumbnail from. The mapping never
  appears in the APNs payload itself.
- **Failure modes:**
  - A `410`/`400` from the transport is treated as a permanent dead token
    (spec §5) and the device row is pruned immediately (`push/engine.py`),
    never retried.
  - MQTT broker disconnects reconnect with capped exponential backoff
    (`compute_backoff`) and back-fill the missed window on resume.
  - Any transport/network error that *isn't* a 410/400 is logged and left
    for the next live event — no retry queue in this version, matching the
    spec's "degrades to no notifications, not a crash" framing.

## Decision override from the spec

The spec's §4 leaves the relay-visible alert text as an open product
question ("New alert on {camera}" vs. fully generic). **This implementation
takes the generic option**: the relay's inputs are exactly
`{device_token, environment, handle, server_id, severity}` — no camera name,
label, or anything content-bearing ever reaches the transport layer, mock or
relay. The specific camera/label/thumbnail are only available after the NSE
redeems the handle from the user's own server.

## Ambiguities resolved while building

- **`review.id` vs. Frigate event id.** The spec's handle example maps to a
  Frigate *event* id (`after.data.detections[...]`), which is distinct from
  the *review* id (`after.id`) used for `apns-collapse-id`. Both are tracked
  on `ReviewEvent` (`event_id` vs. `review_id`); the handle stores the event
  id, the collapse id stays the review id. If a review item somehow arrives
  with an empty `detections` list, `event_id` falls back to `review_id`
  rather than erroring.
- **Backfilled events have no `severity`.** `/api/events` (used for the
  broker-blip back-fill) has no live review-item concept and no `severity`
  field. Resolution: every back-filled event is treated as `severity="alert"`
  — the conservative choice, since a missed alert during an outage is worse
  than one extra low-priority push — with the event's own `label` used for
  the label filter.
- **`server_id`.** Left blank by default and derived from the running
  process at startup (`f"s_{id(app):x}"`) rather than requiring an operator
  to mint one, since the spec only requires it be *stable enough* to route a
  multi-server device's NSE fetch, not globally unique.
