---
title: Push notifications
section: notifications
order: 1
config: ["push"]
---

The sidecar — not Frigate — decides when your phone buzzes. It subscribes to
Frigate's review items over MQTT, evaluates each against your rules, and
sends Apple push notifications through a relay. **{{stat:push_devices}}**
device(s) are currently registered (manage them at
[Settings → Push](/settings#push), which also shows your live ladder table
and example notifications rendered by the real pipeline).

## The attention ladder

Alerts aren't binary. Every detection is scored on a ladder with four rungs:

- **Log** — recorded, no alert.
- **Glance** — a silent notification.
- **Notify** — a notification with sound.
- **Alarm** — critical, breaks through Focus.

The rung comes from a matrix: **who was seen** (unknown person, known
person, animal, vehicle/thing) crossed with the **place class** of the zone
it happened in — a package zone and a secure-area zone earn very different
baseline rungs for the same subject.

Context nudges the rung up or down from there. Nudges *up*: nobody's home,
it's nighttime, the subject is lingering, or they're approaching the secure
area. Nudges *down*: a known face, low detection confidence, or the subject
is leaving. An approaching person can escalate rung by rung, and a situation
that fizzles resolves quietly instead of leaving a stale alarm.

Per-zone overrides set in Zones & routing win over the matrix outright. You
configure the ladder in the Elsinore app (Settings → Alerts) and the app's
choices sync to the sidecar. See your live matrix in
[Settings → Push](/settings#push).

## Configuration

The `push:` config section:

- `enabled` and `transport` — `relay` for real APNs delivery via the push
  relay, `mock` for development.
- `relay_base_url` — where the relay lives.
- `mqtt` host/port — the sidecar must see Frigate's MQTT broker, or no
  events arrive at all.

Devices register themselves: install Elsinore, complete onboarding, and the
phone appears in the device table with a **Test** button.

## If it goes wrong

Check `/healthz` (`mqtt` component) first — no MQTT means no events at all.
Then confirm `transport` isn't still `mock`. Use [Replay](/replay) to
exercise the whole path end-to-end with canned scenarios; its dry-run mode
shows what the ladder *would* send without notifying.
