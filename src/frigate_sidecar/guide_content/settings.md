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
- **Event alignment** (`/settings#alignment`) — fixes event bars sitting a few
  seconds away from their pictures on the scrub timeline. Frigate stamps
  events from the *detect* stream and recordings from the *record* stream —
  two separate camera connections whose clocks drift apart per camera. Press
  **Measure cameras** (it compares recent event thumbnails against the
  recordings; expect a few minutes) and **Apply** the suggested offset for
  each camera with decent confidence. Applied offsets take effect on the next
  timeline refresh, in the sidecar's scrub page and the Elsinore reel alike.
  Setting `detect.annotation_offset` (milliseconds) in Frigate's own
  `config.yml` does the same job, also fixes Frigate's own annotation overlay,
  and wins over anything applied here. Worth doing once at setup and again if
  a camera is replaced or its streams reconfigured.
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
