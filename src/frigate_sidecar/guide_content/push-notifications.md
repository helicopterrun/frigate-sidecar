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
[Settings → Push](/settings#push)).

## The attention ladder

Alerts aren't binary. Each situation earns a rung on a ladder — from
"logged, silent" up through banners, sounds, and time-sensitive alerts —
based on what was seen, where (zone routing policy), and when (quiet hours).
An approaching person can *escalate* rung by rung, and a situation that
fizzles resolves quietly instead of leaving a stale alarm. You configure the
ladder in the Elsinore app (Settings → Alerts) and the app's choices sync to
the sidecar.

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
