---
title: Scrub
section: sidecar
order: 2
routes: ["/scrub"]
config: ["scrub"]
---

[Scrub](/scrub) is the timeline viewer — a **reel**: a fixed aperture over a
column of time that moves behind it. Drag it (down is earlier), scroll, or
use the arrow keys. Frames render instantly, because the sidecar pre-renders
recordings into **sprite sheets** — grids of small frames at a fixed cadence.
The Elsinore app's reel is the same instrument over the same API.

In the last 24 hours the cache generated **{{stat:scrub_sheets_24h}}**
sprite sheets.

## Reading it

Left to right: the time column, then the severity spine, then four object
lanes, then the motion tide coming in from the right edge.

- **Rows** are one rung of the zoom ladder — 1s, 5s, 1m, 5m, 15m or 1h each.
  Those six are exactly the six sprite cadences the cache generates, at one
  cell per row. A row landing on the rung's counting landmark (minute, hour,
  day) gets a heavier rule.
- **Severity spine** is Frigate's own call: a full-width amber bar is an
  alert, a thin grey rule a detection.
- **Lanes** are person, vehicle, animal and package. A track is drawn for as
  long as the object was present, with a cap where it arrived and a cap where
  it left — *no closing cap means it never left*, not that it left there. An
  unrecognised label gets no lane and is not drawn, rather than being filed
  under the wrong one.
- **Tide** is motion: area, low contrast, behind everything, because it
  steers rather than reads.
- **Hatching** means nothing was recorded there. That is distinct from a
  quiet stretch, which still draws its line.

## Using it

- Pick a camera with `?camera=` or the selector.
- Click or tap a track to select it — the card under the image names the
  object, its zones, its sub-label (a recognised face, plate or carrier), its
  score and whether there is a clip to open.
- Toggle lanes to quiet the reel down. Turning the last one off turns them
  all back on; an empty reel reads as broken rather than as a filter.
- Drag the image itself for frame-by-frame.
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

Event bars a few seconds off from the pictures: that's detect-vs-record clock
skew, and it's fixable per camera — see
[Event alignment](/guide/settings) on the Settings page.
