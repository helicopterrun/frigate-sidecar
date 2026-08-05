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

  **Situation pushes use a third relay route.** A situation's title is its
  user-authored name and its body names the label and dwell, none of which a
  fixed severity-keyed template can produce — so the sidecar builds the whole
  APNs body and `send_situation` posts it to `POST
  {relay_base_url}/v1/relay/situation` as
  `{device_token, environment, "apns-collapse-id", payload}`. The relay signs
  the JWT, sets `apns-topic`/`apns-push-type`/`apns-priority` itself, and
  forwards `payload` verbatim; it validates `payload.aps` and 422s anything
  over 4KB. Deliberately *not* `/v1/relay/push`: that route templates its own
  text, so handing it a situation would deliver a generic "New alert" banner
  while reporting success. Implemented in elsinore-push-relay `4278bdf`;
  until that is deployed to Workers a situation send 404s, which surfaces as
  a logged send failure and `502 test_send_failed` from the app's test button
  — visibly broken rather than silently wrong, and v1-shape pushes keep
  working throughout. Plan §8's relay boundary governs: the relay forwards
  these bytes to APNs in flight without persisting, logging, or inspecting
  them, which is what "content-free *at rest*" has always meant. Snapshots
  still never transit it.

  **`apns-collapse-id` is capped at 64 bytes**, by Apple and again by the
  relay, which truncates rather than rejects. `build_collapse_id` trims the
  *situation* id and keeps the track id whole: cutting the tail instead would
  make two people arriving 30s apart share a collapse id, and one
  notification would silently replace the other.

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

## Situations (notification-experience plan, Phase 1)

Implements Phase 1 of Elsinore's `notification-experience-plan-2026-08-05.md`:
the notification primitive moves from "a review item fired" to "a situation is
happening" — a user-authored rule over camera + label + zone + loiter +
time-of-day. Everything not matching a situation is silent as far as *push* is
concerned; the reel and the digests are unaffected.

**Two paths, one deploy.** A device with no `situations` keeps firing exactly
what it fires today (everything above this section). A device with a non-empty
`situations` array switches to situation-only evaluation, its v1
`cameras`/`labels`/`min_severity` surviving as a cheap pre-filter. No phone
loses a push on upgrade.

- **Registration (v2):** `PUT /v1/push/devices/{token}` additionally accepts
  `schema_version`, `timezone`, `location`, `situations`, `snoozes`,
  `live_activity_token`, `morning_digest`, `llm`. The last three are persisted
  and deliberately unread until Phase 2/4. The response echoes the
  `schema_version` the sidecar will actually evaluate the device under, plus
  `situations_accepted` — a rule the sidecar couldn't parse would otherwise
  look enabled in the app and never fire. Omitting `snoozes` leaves existing
  ones alone (the app re-registers on every launch; a launch must not cancel a
  snooze the user set an hour ago). An explicit `[]` clears them.
- **Evaluation:** `push/situations.py` — pure, dependency-free. Only the
  `interrupt` tier has a delivery surface this phase; `present` and `ambient`
  situations parse, persist, and evaluate but do not send, since Live
  Activities (Phase 2) and widgets (Phase 3) are what deliver them. The
  sidecar logs once per device rather than dropping them silently.
- **New endpoints:** `GET /v1/push/situations/library` (starter situations),
  `GET /v1/push/sounds`, `POST /v1/push/snooze`, `DELETE
  /v1/push/snooze/{scope}`, `POST /v1/push/test/{situation_id}`, `GET
  /v1/push/thumbnail/{handle}`.
- **Rate limiting:** max 10 pushes per situation per device per rolling hour
  (`push.rate_limit_per_hour`). Beyond it, matches are suppressed silently and
  the next push that gets through carries a `" · +X more"` suffix. Counted in
  SQLite, not memory, so bouncing the process can't reset a runaway camera's
  ceiling.
- **Pre-warmed thumbnails:** on a match the sidecar pulls the snapshot, resizes
  to ~320px/q60 (~10–20KB) and parks it under the push's handle for 24h; the
  NSE fetches it from `GET /v1/push/thumbnail/{handle}` against an already-warm
  cache. The fetch runs *in parallel* with the send, never in series, and every
  failure path costs the notification its image rather than its existence.

### Loiter needs `frigate/events`, not `frigate/reviews`

The plan derives dwell by holding a first-seen timestamp against subsequent
`frigate/reviews` `type: update` messages. Measured against this deployment
(19.6 min of live traffic, 2026-08-05) that topic published **4 messages**:
two review items, each a `new` and an `end` ~30s apart, with no `update`
between them. Frigate publishes a review update when the item's *data* changes
— a new object, a new zone, a severity promotion — not on a clock, so a person
standing still is exactly the case that generates no traffic. A loiter
threshold fed only from there is never re-evaluated and never fires.

`frigate/events` published 2031 messages over the same window (~0.2–0.5s per
object) and carries `current_zones` — live occupancy, which *drops* a zone when
the object leaves, unlike the review topic's cumulative `zones`. So dwell comes
from there: entry timestamps that reset on a real exit, and a tick to
re-evaluate against.

`frigate/reviews` remains the sole authority on whether anything is
push-worthy — a `frigate/events` message can only fire a situation for a track
some review message already declared alert-worthy. Set
`push.dwell_source: reviews` to restore the literal prescribed behaviour.

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
