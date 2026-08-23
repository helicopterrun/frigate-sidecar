---
title: Troubleshooting
section: operations
order: 2
routes: ["/debug", "/toybox", "/login"]
---

## First stops

- [`/healthz`](/healthz) — one-word status per component. Workers report
  **staleness**, so a wedged loop shows up even while the process lives.
- [Status](/) — the same picture, visually, with sizes and probes.
- [Debug](/debug) — version, the live capability probe, and the interactive
  API docs.

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

## The toybox

[/toybox](/toybox) is a 50-states map quiz with a high-score board. It has
no operational purpose whatsoever. High scores are, however, persistent.
