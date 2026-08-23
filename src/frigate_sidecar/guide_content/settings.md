---
title: Settings
section: sidecar
order: 5
routes: ["/settings"]
config: ["watchdog", "proxy"]
---

[Settings](/settings) mirrors the Elsinore app's settings screen and gathers
the sidecar's own knobs in one place.

## Using it

- **Zones** (`/settings#zones`) — the zone routing policy that decides which
  zones matter for alerts, zone neighbor relationships, and export/import of
  the whole policy as JSON.
- **Push devices** (`/settings#push`) — every registered phone, with per-
  device **Test** buttons, plus the live attention-ladder table and example
  notifications rendered by the real pipeline. Currently
  **{{stat:push_devices}}** device(s) are registered. See
  [Push notifications](/guide/push-notifications).
- **Cameras** — per-camera rig facts (position, heading, FOV, calibration
  quality) with deep links into the [Map](/map) landmark editor.
- **Faces** — a read-only view of the face pipeline configuration.
- **Appearance** — the theme picker (five palettes shared with the app).
- **Help** — a link to this guide; on phones this is the guide's front door.
- **About** — version and debug links.

## Configuration

- `watchdog:` — an optional external health watchdog that can restart the
  Frigate container when it wedges. It runs as its own systemd unit and is
  off by default; the sidecar itself never restarts anything.
- `proxy:` — behavior of the reverse proxy through which your browser
  reaches Frigate's API (timeouts, header handling). Defaults are fine for
  almost everyone.
