---
title: Troubleshooting
section: operations
order: 2
routes: ["/debug", "/toybox", "/login"]
---

## First stops

- [`/healthz`](/healthz) — one-word status per component. Workers report
  **staleness**, so a wedged loop shows up even while the process lives.
  `mqtt` is connected/disconnected; `scrub` is ok/starting/stale/locked
  (`locked` means another process — a restarting predecessor or a concurrent
  `fsc scrub` invocation — holds the cache lock, distinct from a wedged
  loop); `face_enrich` is ok/starting/stale the same way. `frigate` is
  informational only (ok/error/unreachable) and never flips the overall
  status or HTTP code — a Frigate outage is watchdog's job, not a reason to
  restart the sidecar. Any check going bad (except `frigate`) returns
  HTTP 503 instead of 200.
- [Status](/) — the same picture, visually, with sizes and probes.
- [Debug](/debug) — version, the live capability probe (the same payload
  the iOS client reads), and a link to the interactive OpenAPI docs. Check
  here first when a client integration reports a missing/unexpected
  capability, or to confirm which build is actually running.

## Common symptoms

| Symptom | Usual cause |
|---|---|
| Scrub strips empty | `media_path`/`recordings_path` mapping wrong, or camera not enrolled in `scrub.cameras` |
| No push notifications | MQTT unreachable (check `mqtt` in `/healthz`), or transport still `mock` |
| Login loop / 401s | `frigate.proxy_base_url` pointing at the wrong origin |
| Triage pages empty on a dev box | No Frigate database — pages say so rather than erroring |
| Identities never appear | `face_enrich.enabled` off, camera not enrolled, or the `enrich` extra not installed |

## Signing in

When `require_frigate_auth` is on, every page needs a Frigate session. The
[login page](/login) posts your credentials straight through to Frigate —
the sidecar never sees or stores your password — and can mint a
stay-signed-in cookie.

Repeated failed logins from the same IP get a **429** with a `Retry-After`
header once they exceed the configured attempt window — only failed
attempts against the login endpoint itself count (a client's parallel
401 retries against other pages never trip this). Wait out the window
before trying again.

## Backup & restore

`fsc backup <dest>` writes the sidecar DB, session secret, and resolved
config to a directory or `.tar.gz` (scrub cache and face-model directories
are excluded — both regenerate from Frigate's own data). `fsc restore <src>
--force` restores one; stop frigate-sidecar first.

## The toybox

[/toybox](/toybox) is a 50-states map quiz with a high-score board. It has
no operational purpose whatsoever. High scores are, however, persistent.
