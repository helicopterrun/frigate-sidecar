---
title: Status & live view
section: sidecar
order: 1
routes: ["/", "/live/{camera}"]
---

The [Status page](/) is the landing dashboard. It refreshes itself every few
seconds and shows:

- **Service health** — each sidecar component (database, MQTT subscriber,
  scrub cache worker, face enrichment worker) with staleness detection: a
  worker that stops completing cycles turns red even if the process is alive.
- **Storage** — database and cache sizes.
- **Cameras** — one tile per camera; tap a tile to open its live view.

## Live view

`/live/{camera}` plays the camera's stream directly in the browser using
WebRTC (falling back to MSE where WebRTC can't connect). This is the same
low-latency path the Elsinore app's Live tab uses.

On a phone, the Status page is the first tab in the bottom bar — it works
well as the "glance" screen when the sidecar is installed to your home
screen as a web app.
