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

  **Live Activities need a fourth relay route.** `/v1/relay/situation`
  hardcodes `apns-push-type: alert` and `apns-topic: env.APNS_TOPIC`
  (elsinore-push-relay `43c5209`). A live-activity push differs in all three
  of the fields that matter — `apns-push-type: liveactivity`, the
  `<APNS_TOPIC>.push-type.liveactivity` topic, and a token that is neither the
  device's alert token nor the same one for start vs update — so
  `send_live_activity` posts to `POST {relay_base_url}/v1/relay/liveactivity`
  as `{device_token, environment, "apns-collapse-id", event, payload}`.
  `event` is `start` | `update` | `end`, passed so the relay can validate the
  shape (a `start` must carry `attributes` and `attributes-type`) rather than
  letting Apple 400 it. **Not yet implemented in elsinore-push-relay** — until
  it is, LA pushes 404 and Present-tier situations are silent on
  LA-capable devices, while every Phase 1 path keeps working.

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
- **`sent_at`:** every situation payload carries unix epoch seconds to the
  millisecond, stamped in `build_payload` at the last moment the sidecar
  controls before the bytes leave for the relay. The app's NSE subtracts it for
  `sidecar_to_nse_ms` / `sidecar_to_present_ms` — the only way to see the APNs
  hop from outside, since Apple gives no delivery receipt. Sub-second on
  purpose: whole seconds would quantise a measurement whose interesting range
  is hundreds of milliseconds. Not present on v1-path or plain-test pushes,
  whose bodies the relay templates (see the relay note above).
- **Pre-warmed thumbnails:** on a match the sidecar pulls the snapshot, resizes
  to ~320px/q60 (~10–20KB) and parks it under the push's handle for 24h; the
  NSE fetches it from `GET /v1/push/thumbnail/{handle}` against an already-warm
  cache. The fetch runs *in parallel* with the send, never in series, and every
  failure path costs the notification its image rather than its existence.

## Attention ladder (Elsinore Phase 1: routing engine)

`frigate_sidecar/push/ladder.py` answers one question, statelessly: given a
detection snapshot, how loud is it? It has no APNs, delivery, Live Activity,
or HTTP surface of its own — later phases wrap `evaluate_ladder` rather than
duplicate its policy.

- **Output:** one of four ordinal attention levels, `log < quiet < notify <
  urgent` (`ladder_policy.LEVELS`), or `ladder.SUPPRESSED` for a muted
  snapshot.
- **Input:** `ladder.Snapshot` — a pre-classified `subject`
  (`stranger`/`known`/`animal`/`thing`) and `place`
  (`street`/`yard`/`doors`/`private`/`off_limits`), the raw Frigate `label`,
  and a set of context/exception booleans. `source == "system"` (e.g. a
  camera-offline card) has no subject or place and short-circuits to a fixed
  level instead of touching the table.
- **All policy is data, in `ladder_policy.py`:** the subject x place base
  table, which reasons nudge the result up (`WORRY_REASONS`) or down
  (`CALM_REASONS`), which Frigate labels reclassify as `stranger`
  (`DANGEROUS_ANIMAL_LABELS`), and the system-card level
  (`SYSTEM_CARD_LEVEL`). Retuning any of these is a data edit; `ladder.py`
  itself should not need to change.
- **Evaluation order** (see `ladder.py`'s module docstring for the full
  rationale): mute → system-card short-circuit → safety exceptions
  (`audio_safety`, `ai_flagged`, unconditional `urgent`) → dangerous-animal
  reclassification → base table lookup → one net-worry/calm nudge (`animal`
  never nudges; `known` never nudges up) → child-hazard-zone floor (at least
  `notify`) → `street`/unconfirmed-detector caps (at most `quiet`).

**To change policy:** edit `ladder_policy.py`, then run the golden suite:

```sh
pytest tests/test_push_ladder.py
```

`fixtures/ladder/ladder_cases.json` is the golden suite — one row per
precedence rule the engine has to get right (e.g. "a known-subject nudge never
crosses the up direction", "a safety exception beats the street cap"). It's
hand-authored, not generated: a policy change that alters a case's outcome
must update its `expected` value deliberately, in the same commit as the
policy edit, not be silently re-blessed.

## Attention ladder: delivery pipeline (Elsinore Phase 2)

*Not to be confused with the "Live Activities (Phase 2)" section below --
that is the notification-experience plan's own Phase 2, already shipped.
This section is the attention ladder project's Phase 2: the stateful layer
that wraps Phase 1's `evaluate_ladder` (previous section) and turns a
stream of detection snapshots into ordinary alert/silent APNs pushes.*

`push/cards.py` (pure), `push/card_store.py` (sqlite), `push/delivery.py`
(payload + orchestration), and `push/delivery_wire.py` (live wire-up) --
config-gated behind `push.delivery_enabled` (default **off**), independent
of `push.enabled` and the situations/Live-Activity machinery below. It
ships dark: enabling it does not change anything about the v1 or v2
(situations) paths, which are untouched.

### The card model

A **card** is the unit of user-facing state: one card per subject. Five
detections of the same person over two minutes mutate one card, they don't
create five. `card_key` is stable per ongoing subject and doubles as the
`apns-collapse-id`:

```
{camera}:{subject_kind}:{tracked_object_id-or-opening-id}
{camera}:system:{reason}                                          # system cards
```

**Zone is deliberately not part of identity**, unlike the design brief's
literal example. An earlier version keyed on
`{camera}:{zone}:{subject_kind}:{subject_id}`; the first supervised live run
(2026-08-08) showed exactly the failure that predicts -- a car first seen
with no zone, then entering `parking_spot`, produced two cards
(`alley-wide:_:thing:...-m0d7oe` then `alley-wide:parking_spot:thing:...
-m0d7oe`) for the same tracked object, because the zone-bearing key changed
out from under it. Zone still travels on every payload as `zone_name` and
still drives mutation classification when it changes the routed level (an
enrich, or an escalate/deescalate if the level moves) -- it's mutation
context, not identity. See `delivery.build_card_key`'s docstring.

A card (`cards.Card`) holds exactly what the mutation classifier and sound
accounting need: `level`, `created_at`/`updated_at`, `state_since_at` (when
the *current* level became true -- resets on create/escalate/deescalate,
held steady across enrich/resolve), `sound_count`, `handled`/`handled_at`,
`last_sound_at`, `resound_count`, `resolved`, `closed`. Everything else
(camera, zone, copy) is threaded through the payload builder by the caller
rather than duplicated on the card -- see `card_store.list_open_urgent_cards`
for where that context is actually persisted (the `push_cards` table has
it; the `Card` dataclass doesn't).

### Mutation classification

Each new ladder evaluation against a card key is classified
(`cards.classify_mutation`):

| Mutation | Condition | Push |
|---|---|---|
| `create` | no existing (or closed) card | alert at the routed level, sound per budget |
| `enrich` | same level, new facts | silent, same collapse id |
| `escalate` | new level > old level | alert with sound, subject to the budget |
| `deescalate` | new level < old level | silent, same collapse id |
| `resolve` | explicit `resolved=True` signal | silent, never a sound |
| `suppressed` | ladder returns `SUPPRESSED` (muted) | no push; card closes |

**`resolved` is not derived from the level.** A `thing` at a non-street
place never evaluates below `quiet` (`ladder_policy.TABLE`), so there is no
`new_level` a caller could pass that would mean "the subject is gone" --
that has to be an explicit signal from whatever the real end-of-condition
source is (a Frigate object `end`, a door-closed sensor, `frigate/available`
coming back). `advance_card(..., resolved=True)` is how the wire-up says so;
`classify_mutation`'s docstring has the full rationale. Mute beats resolve,
matching the ladder's own "mute beats everything" rule.

### Sound accounting -- the entire anti-spam policy

- Sound **at most twice per card**: once at `create` (only if the level is
  `notify`/`urgent` -- a `quiet` create never sounds), once at the first
  `escalate` past `quiet`.
- Budget is spent by *sounds emitted*, not by beats: a silent `quiet`
  create doesn't spend it, so the card's first-ever escalation can still be
  sound #1, and a second escalation (`quiet` → `notify` → `urgent`, say)
  can legitimately be sound #2.
- Further escalations after the budget is spent still update level and
  content, silently.
- **Urgent re-sound, once:** an `urgent` card unhandled after
  `push.delivery_urgent_resound_s` (default 120s) may re-alert exactly
  once (`cards.urgent_resound_due`, `delivery.apply_urgent_resound`),
  config-gated by `push.delivery_urgent_resound_enabled` (default on).
  "Handled" is a hook, not a solved problem -- multi-device dismissal sync
  doesn't exist yet, so today only the sidecar's own timer sets `handled`
  (there is no client-facing "mark handled" endpoint in this phase). The
  re-sound is a third, urgent-only sound; it doesn't touch `sound_count`.
- All accounting lives on the card row, keyed by `card_key`.

### Level → APNs mapping

| Level | Push? | `interruption-level` | Sound |
|---|---|---|---|
| `urgent` | yes | `time-sensitive` | default sound (no Critical Alerts entitlement -- `critical` is never attempted) |
| `notify` | yes | `active` | default sound |
| `quiet` | yes | `passive` | none |
| `log` | no | -- | -- (recorded for the timeline/digest, out of scope this phase) |
| `suppressed` | no, card invisible | -- | -- |

Silent mutations reuse the same alert-push channel with `aps.sound` omitted
and the same `apns-collapse-id`, so the card replaces in place --
`content-available` background pushes are not needed for this.

### Payload contract

`docs/apns-payload-spec.md` is the versioned (`"v": 1`) contract the app and
NSE build against: `card_key`, `mutation`, `level`, `subject_kind`,
`place_class`, `camera`, `zone_name`, a semantic `glyph` id (icon mapping is
entirely client-side), `primary`/`secondary` copy (state-what-is-true
grammar -- never asserts an identity that hasn't resolved), `event_ts`,
`state_since_ts`, and optional `media`/`deep_link`. `delivery.build_card_payload`
implements it; `delivery.py`'s module docstring has the layering.

### Wire-up (ships dark)

`push/delivery_wire.py` hooks into the two entry points every
`frigate/reviews` and `frigate/events` message already passes through --
`PushEngine.handle_event` and `handle_object_payload`'s object-end branch --
guarded by `push.delivery_enabled`. No new transport (sends through the
existing `PushTransport.send_situation`, same as the situations payload)
and no new MQTT subscription.

**Snapshot media** (`media`, `docs/apns-payload-spec.md`): on `create`/`enrich`
only (never `escalate`/`deescalate`/`resolve` -- see the spec doc for why),
`delivery_wire._media_for` mints a handle (`store.mint_handle`, same table
situations use) and fires `PushEngine.prewarm_thumbnail` concurrently with
the send, exactly like the situations path's `_fire_group` -- the push
carries the URL optimistically and a slow/failed Frigate fetch costs the
notification its image, never its existence. Unlike situations (which send
just `handle` + `server_id` and let the already-registered app resolve the
base URL itself), the card contract documents `media` as one complete,
self-authorizing URL, so building it needs the sidecar's own phone-reachable
address -- `push.external_base_url` (empty by default; `media` is simply
omitted until it's set to something real, e.g. `http://192.168.50.207:5001`
on this deployment). This is never Frigate's own address -- Frigate stays
LAN-internal (`frigate.base_url`) and the sidecar re-hosts the fetched
snapshot behind the handle, same trust boundary as every other push image
this codebase sends.

**Subject/place classification is a deliberate MVP**, not the full-fidelity
mapping the ladder deserves: `classify_subject` reads `person` +
`sub_labels` (known if present, stranger if not -- never the reverse) or an
animal-label set off `frigate/reviews` alone; `classify_place` looks up
`event.zones` against `push.delivery_zone_place_map` (falling back to
`yard` if any zone is present, else `street`). Both are heuristics,
documented as such, safe to ship because the whole pipeline defaults off.
Tightening them is a config/data change in `delivery_wire.py`, not a change
to `ladder.py` or `delivery.py` -- the same policy/evaluation split
`ladder_policy.py` already established.

Resolution rides on `frigate/events`' object `end` message
(`handle_delivery_resolve`) rather than being derived from a review, since
that is the actual "the subject is gone" signal Frigate provides (see the
mutation-classification note above on why a ladder level can't say this by
itself). It re-checks every subject-kind's card key for that track id,
since the object stream alone doesn't carry which kind a review classified
it as.

The one-time urgent re-sound runs on its own sweep
(`_delivery_resound_sweep_loop` in `server.py`, interval
`push.delivery_resound_sweep_interval_s`, default 15s) -- the same "only
ever tightens, never a keep-alive" shape as the Live Activity resolution
sweeper below.

### Cross-camera deduplication

Overlapping fields of view mean the same physical event can produce several
independent cards -- one per camera that happens to track it -- since each
camera runs its own tracker with its own `track_id`. The first supervised
run's own numbers made this concrete: one person walking the property
generated five stranger cards in 30 seconds across `stairway-wide`,
`stairway-tight`, `alley-wide`, `walkway`, and `street`.

Frigate zone names are the correlation signal: two cameras with a zone
named e.g. `driveway` are assumed to see the same physical space. When a
*fresh* `(camera, track_id)` -- one with no card of its own yet -- carries a
zone, `delivery_wire._resolve_card_for_track` looks for an open card with
the same `subject_kind` and `zone_name` created within the last 15 seconds
(`_DEDUP_WINDOW_S`; real-world gaps between cameras picking up the same
walk-through measured 3-4s, so 15s is deliberately generous without risking
merging genuinely separate events). If one exists, this track is *aliased*
onto it (`push_card_track_aliases`, keyed on `(camera, track_id)`) instead
of minting a new card key -- every later event for this track routes
straight to the merged card via the alias, without re-running the query.
The merged card's `camera` field stays whichever camera created it first
(the app's timeline routing depends on that field naming a real,
resolvable camera); the enriching camera is surfaced in the copy instead
(`"... · also on {camera}"`).

A subject changing zones mid-lifetime on its *own* already-existing card
(no dedup involved -- see the zone-identity note above) just enriches that
card's `zone_name` in place, the same as any other mutation; it never
mints a new key. Three or more cameras sharing a zone all merge onto
whichever card is oldest, not whichever alias was looked up last.

No dedup is attempted when either side has no zone at all (Frigate hasn't
told us where the object is, so there's nothing to correlate on) or when
`subject_kind` or `zone_name` differ -- those always get independent cards.
The 15s window is measured from the *candidate* card's `created_at`, so two
detections of the same subject arriving more than 15s apart (a genuinely
separate visit) correctly get separate cards.

Resolution stays asymmetric on purpose: an aliased (non-owning) track
resolving just drops its alias silently -- the owning camera may well still
be tracking the subject, so the shared card is not touched. Only the
*owning* camera's track resolving (its own natural card key, no alias)
resolves the card, exactly as it always did before dedup existed. If a
still-tracking secondary camera keeps reporting after that, it gets a fresh
card under its own key on the next event, per the ordinary create path --
by then it will typically have moved to a different zone anyway (the same
walk-through scenario that motivated this feature in the first place).

No configuration knob for v1 -- the zone-name-equality assumption is
undocumented policy, not a setting: a camera that happens to reuse a zone
name for a genuinely different physical space (a coincidence, not a
correlated view) would incorrectly dedup against it. That's an explicit
non-goal this phase, same spirit as every other MVP heuristic in this
module.

### Tests

`fixtures/ladder/delivery_cases.json` + `tests/test_push_delivery.py`
extend the ladder's golden-suite approach to **sequences**: each case is an
ordered list of snapshots (plus, for the urgent case, timer checks and a
"mark handled" event) against one card key, with an expected
`(mutation, level, sound, push)` row per step. `tests/test_push_cards.py`
and `tests/test_push_card_store.py` cover the pure classifier/sound-budget
logic and the sqlite persistence directly; `tests/test_push_delivery_payload.py`
covers payload construction and the send/sweep orchestration against
`LogTransport`.

### Not built this phase

The daily digest and any settings/config UI surface for cards -- out of
scope, per the design brief. `ladder.py`/`ladder_policy.py` are untouched.
(Live Activity start/update/end and pushToStart tokens for cards *were*
this phase's own "not built" list -- see "Live Activities for cards" below,
Elsinore Phase 3.)

## Live Activities for cards (Elsinore Phase 3)

*Not the same feature as "Live Activities (Phase 2)" below -- that is the
notification-experience plan's own Live Activity for **situations**
(`push/activity.py`, `ATTRIBUTES_TYPE = "SituationActivityAttributes"`,
already shipped). This is a Live Activity for **cards** (`push/cards.py`,
the previous section), an additional output channel on the same card
lifecycle -- create/enrich/escalate/deescalate/resolve -- the ordinary card
push already drives. The two run independently and share nothing but the
`push_activities` table.*

`push/live_activities.py` (pure: family detection, glyph mapping, the three
payload shapes) plus the wiring inside `push/delivery_wire.py`
(`_deliver_live_activities`), gated by `push.delivery_la_enabled` (default
**on**, but only ever reachable through `push.delivery_enabled` -- an LA is
additive to a card, never a substitute for it).

### Family detection

A card qualifies for a Live Activity when it maps to one of four families
(`live_activities.should_start_activity`, MVP hard-coded rules):

| Family | Rule |
|---|---|
| `package` | `subject_kind == "thing"` and label `"package"` |
| `bins` | `subject_kind == "thing"` and label `"waste_bin"` or `"garbage_truck"` |
| `openings` | `subject_kind == "thing"` and label in `{"door", "gate", "garage"}` |
| `person` | `subject_kind in ("stranger", "known")` and `place_class == "doors"` |

`push.delivery_la_families` (`{family: bool}`, absent or `True` = enabled)
is checked last, wrapping detection rather than replacing it -- a disabled
family's cards never start an activity even when they'd otherwise match,
but the rules above don't need config to exist at all. One activity per
`(device, card)`: a card already running one for a device gets updates, not
a second start.

### Push-to-start, updates, and end

Three payload shapes (`live_activities.build_la_start_payload` /
`build_la_update_payload` / `build_la_end_payload`), all
`apns-push-type: liveactivity`, `apns-topic:
com.houseofpaimon.Elsinore.push-type.liveactivity`:

* **start** -- to the device's push-to-start token, on a qualifying card's
  `create`. Carries `attributes`/`attributes-type` (`"ElsinoreActivityAttributes"`
  exactly -- the Swift type ActivityKit routes the push by) and
  `content-state`.
* **update** -- to the per-activity token the app uploads via the existing
  `POST /v1/push/activity/token` after the activity starts, on every later
  mutation (`enrich`/`escalate`/`deescalate`). Silent by construction (no
  `alert` key).
* **end** -- on `resolve`. `dismissal-date` is `timestamp + 4`: the resolved
  state shows briefly, then iOS clears it from the lock screen (the
  activity itself lingers in the recent-activities area up to 4h more,
  system-controlled). Sent via the per-activity token if the app ever
  uploaded one, else the push-to-start token -- APNs accepts an `end` push
  there too, so a card resolving before the token ever arrives still
  dismisses the activity instead of leaving it stuck open.

`content-state` (`live_activities.build_content_state`) field names are
snake_case to match the Swift type's `CodingKeys` exactly: `level`,
`mutation`, `glyph`, `primary`, `secondary`, `elapsed_seconds`,
`deep_link_card_key`, `thumbnail_handle`, `thumbnail_revision`.
`elapsed_seconds` is `int(now - card.state_since_at)`, the same clock the
ordinary card payload's `state_since_ts` reports. `glyph`
(`live_activities.glyph_for`) is a semantic SF-Symbol-or-documented-custom-
name id; `resolve` always reports `checkmark.circle.fill` regardless of
family, checked before any family-specific branch.

**Thumbnails reuse the existing handle/prewarm infrastructure unchanged**
(`_media_for`, same section above): the bare handle minted for the ordinary
card push's `media` field is threaded through as `thumbnail_handle` too,
bumping `thumbnail_revision` whenever a fresh one is minted (never on
`escalate`/`deescalate`/`resolve`, same `_MEDIA_MUTATIONS` gate). No second
handle is minted for the same snapshot.

### Where the activity lives: `push_activities`, not a `push_cards` column

Reuses the *situations* Live Activity's own table and `store.py` helpers
(`open_activity`/`find_activity`/`touch_activity`/`close_activity`/
`record_activity_send`) rather than adding a column to `push_cards`.
`situation_id` is just a text column, and a card's `card_key` fits it
exactly as well as a situation id does. This is a deliberate departure from
a `push_cards.la_activity_id` column: a Live Activity is one per
**(device, card)**, and `push_cards` is one row per card -- a card with two
push-to-start-registered devices runs two independent activities on two
different tokens, which a single nullable column cannot represent, but
`push_activities`, keyed on `(apns_token, situation_id, track_id)`, already
does. The track id used for that key is parsed back out of `card_key`
(its final `:`-separated component) rather than whatever camera's event
triggered the current mutation -- stable across cross-camera dedup, so a
card merged onto by a second camera keeps updating the same activity
instead of forking a second one that would never get a token.

### What ends an activity

`resolve` is the only path that ends a card-model activity -- there's no
separate resolution sweep the way the situations Live Activity has one,
because a card's own resolve signal (`delivery_wire.handle_delivery_resolve`,
`frigate/events` object `end`) already is that authoritative "gone" signal.
Cross-camera dedup's asymmetric resolve applies unchanged: a merged
(non-owning) track resolving drops its alias and touches neither the card
nor its activity; only the owning track's resolve ends both.

### Tests

`tests/test_push_live_activities.py` covers the pure logic (family
detection, glyph mapping, the three payload shapes) directly.
`tests/test_push_live_activities_wire.py` runs full lifecycles through
`handle_delivery_event`/`handle_delivery_resolve` against `LogTransport`:
create → enrich → escalate → resolve with incrementing `elapsed_seconds`
and the right event/mutation at each step, a non-qualifying card producing
zero activity pushes, a device with no push-to-start token getting no
activity while its ordinary card push is unaffected, a late per-activity
token dropping (not buffering) the update it missed, a resolve landing
before any per-activity token arriving falling back to the push-to-start
token, and cross-camera dedup producing exactly one activity for the
surviving card.

## Live Activities (Phase 2)

Present-tier situations stop being silent and start living in a Live Activity:
the object enters the zone, the Dynamic Island grows a snapshot and a timer,
and only if the situation crosses its own escalation bar does anything buzz.

**Three tokens, three purposes.** The alert token (Phase 1, the URL path of the
registration) still carries alerts. `push_to_start_token` — one per app
install, on the registration record — creates activities. A *per-activity*
token, uploaded by the app once iOS mints it, carries updates and the end.

**The stage machine** (`stage` on the per-track store, mirrored on the
`push_activities` row):

| Transition | Push | Token |
|---|---|---|
| conditions first match → `arriving` | start | push-to-start |
| still dwelling → `present` | update (silent) | per-activity |
| escalation trigger → `escalated` | **update-shape LA push + `alert` + `sound`** | per-activity |
| zone exit / object end / 30s quiet → `ending` | end + `dismissal-date` | per-activity |

- **The activity starts before the loiter threshold.** Loiter decides the
  *interrupt*, not the activity — plan §3 has the LA appear at "0:04" and
  escalate at five seconds.
- **Escalation is one live-activity push that also buzzes** — an `update`
  shape carrying `alert` and `sound` at the `aps` level, which iOS 17.2+
  delivers as a single event: ContentState advances, banner shows, sound
  plays. It is *not* an alert push with a matching collapse id: that collapses
  in Notification Center but cannot advance a Live Activity's ContentState, so
  the banner and the activity would drift apart (plan amended, Elsinore
  `98e447e`). The alert shape survives only as the fallback for a device with
  no activity to advance — start failed, or the per-activity token hasn't been
  uploaded yet. Buzzing without advancing beats not buzzing.
- **Only Present-tier situations run activities.** Interrupt-tier ones are
  Phase 1, unchanged. As of Phase 2 the `at-the-door` starter ships as Present
  with `escalation: loiter_exceeds:5` — the LA experience's poster child, and
  it cannot be that while authored at Interrupt, which fires once and is over.
- **Fallback:** a device with situations but no `push_to_start_token` gets
  Phase 1-shape alert pushes for its Present-tier situations. "The app works
  without Phase 2."
- **Budgets:** LA pushes are metered separately (`activity_updates_per_hour`,
  60) from the alert ceiling (10), and updates are coalesced to one per
  activity per `activity_update_min_interval_s` (3s). Updates fire on
  `frigate/events` observations, never on a timer — the only clock-driven job
  is the resolution sweeper, which exclusively *ends* activities.
- **Early fire** (`detection_tier_early_fire`): the activity also starts on a
  `detection`-severity review, ~500ms ahead of the `alert` promotion, and the
  severity pre-filter is relaxed for exactly those situations. One that never
  promotes ends with a 10s tail instead of 30s.

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
