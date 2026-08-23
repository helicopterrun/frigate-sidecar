---
title: Scrub
section: sidecar
order: 2
routes: ["/scrub"]
config: ["scrub"]
---

[Scrub](/scrub) is the timeline viewer: drag across the strip and frames
render instantly, because the sidecar pre-renders recordings into
**sprite sheets** — grids of small frames at a fixed cadence. The Elsinore
app's Scrub reel uses exactly the same cache over the same API.

In the last 24 hours the cache generated **{{stat:scrub_sheets_24h}}**
sprite sheets.

## Using it

- Pick a camera with `?camera=` or the selector.
- The timeline shows recording coverage; gaps mean Frigate recorded nothing.
- Scrubbing near "now" works too — the cache continuously follows the live
  edge, so the newest minute is only a few seconds behind.

## Configuration

The `scrub:` config section:

- `enabled` + `cameras` — which cameras get cached. Each camera costs disk
  and steady CPU, so enroll the ones you actually scrub.
- `cache_dir` — where sheets are written. Must be a real local filesystem
  path with room to grow.
- Interval/thinning/retention settings trade freshness and history depth for
  disk. The defaults follow the design spec and rarely need touching;
  retention pruning runs automatically.
- `preserve_source_aspect` / `format` control how cells are rendered.

## If it goes wrong

Empty strips: check `/healthz` (`scrub` component) and the
`media_path`/`recordings_path` mapping described in
[First run](/guide/first-run). A camera missing from the picker usually
isn't enrolled in `scrub.cameras`.
