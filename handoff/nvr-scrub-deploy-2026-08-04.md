# Task: deploy the scrub-sheet fixes on the NVR and verify them on real data

You are running on the NVR box (Ubuntu, `192.168.50.207`) with root access. It
runs Frigate plus `frigate-sidecar` against ten cameras: `alley-wide,
crows-nest, doorbell, garden, gate, package, stairway-tight, stairway-wide,
street, walkway`.

Work was done on a separate development Mac and pushed to `main` in
`github.com/helicopterrun/frigate-sidecar`. It could not be verified against
this deployment because that machine had no access here. That verification is
your job, and parts of it can only be done here.

**Read this whole document before acting.** Do not treat the claims in it as
established on this box — several are measurements from a two-camera demo
instance and are exactly what you are here to confirm or refute at ten-camera
scale.

## Background: the two problems that were fixed

### 1. Sheets declared coverage for cells they had padded black

A scrub sheet is a 12x8 grid of stills. `scrub_sheets.count` says how many cells
it holds; the client maps a timestamp to a cell index and crops it. The tiler
composes onto a **black canvas** and pastes each cell at its own index, so any
index below `count` with no cell file on disk is served as a black frame — while
the index tells the client it is real imagery.

`count` used to be derived from assignment indices (`max(idx) + 1`), not from the
files actually on disk, so the two could disagree. The client reported a
confirmed black frame on `stairway-tight` at unix `1785819272`, attributed to
`/v1/scrub/stairway-tight/sheet/1785816900-60.0-45.jpg`, cell at pixel origin
(960, 540) — which is cell index 39 (960/320 = col 3, 540/180 = row 3, 3x12+3),
grid time `1785816900 + 39*60` = 21:54:00 local, inside `count=45`.

The holes themselves came from `grid.assign_cells` measuring cell contiguity
against `accepted[-1]`, which is empty at the start of every call — so a segment
contributing a single late frame was accepted at its own index and the cell
before it was never filled. Fixed by splitting the bucket instead.

Publication now measures the count from the cell store: the contiguous run of
cell files present from index 0, scanning the whole grid.

### 2. The trailing edge was serviced too slowly

The generation loop ticked at `generate_interval_s` with the backfill phase
*inside* the tick. The live-edge pass had grown to ~65 s (it feeds the coarse
tiers from the same decode) and backfill's budget landed on top, giving a ~100 s
effective cadence and ~105 s measured worst lag — past the ~90 s the client is
told to expect. Cadence is the freshness bound: a camera serviced at the top of a
tick is untouched until the next one.

The tick is now a **deadline**. The trailing-window pass runs at the top of every
`live_edge_interval_s` and backfill gets the remainder (still capped by
`backfill_time_budget_s`); a tick that overruns is not slept off.

## What is already on `main` (five commits)

| Commit | What it does |
|---|---|
| `fb3549a` | Bucket splits on a non-adjacent frame; `count` measured from the cell store |
| `2a26412` | `fsc scrub verify [--repair]` — fixes sheets already in the index |
| `86f616a` | Tick-as-deadline, `live_edge_interval_s`, `sheet_version_grace_s` sweep |
| `57a6e75` | Tests pinning the sweep's `(camera, interval, start)` key boundaries |
| `e43b276` | Two defects an adversarial review found (below) |

New config knobs, both in `ScrubSection`:

- **`live_edge_interval_s`** (default `20.0`) — trailing-window cadence, and the
  loop's tick. `generate_interval_s` is now the *ceiling* on that tick
  (`min` of the two), so it stays meaningful.
- **`sheet_version_grace_s`** (default `900.0`) — how long a *superseded,
  incomplete* sheet version stays servable before the retention pass sweeps it.
  Complete sheets are never superseded and never swept.

Two defects were found by review after the first push and fixed in `e43b276` —
mentioned because they show the shape of mistake this code invites:

- The sweep's grace was measured from a version's own publication mtime rather
  than from when it stopped being current. Those diverge whenever a sheet is the
  only version of its span for a while, which is normal under backfill's
  round-robin. It now reads the *current* version's mtime instead.
- `_publish_sheet_version` scanned `range(count)` — what the calling pass
  touched — so a backfill pass that filled only a missing cell stopped one past
  the hole and stranded the real cells beyond it. It now scans the whole grid.

## What was verified, and where

Everything below was measured on a **two-camera demo Frigate on the development
Mac**, not here. Treat as unconfirmed at this scale.

- 328 unit tests pass, ruff clean.
- Fine tier (1 s) held a trailing edge **28–40 s** behind wall clock across eight
  20 s ticks, one camera.
- **Zero** black-padded-but-indexed cells across 4 878 indexed cells / 224 sheets.
- A moment 180 s old resolved through the index to a real cell (mean brightness
  397 against a black cell's ~0).
- Cost per camera: **308 ms** per 12.3 s segment full-decoding a 1 s tier (~2.5%
  of one core); **68 ms** keyframe-only where the GOP already matches. About
  **49 MB** of sheets per camera-hour after the sweep.
- `fsc scrub verify --repair` on a deliberately damaged sealed 60 s sheet:
  declared 19 with cell 9 black -> republished at `count=9`, old URL removed.
- Pixel-path scan throughput ~10 ms/sheet, so a full scan of ~36 000 sheets
  should take roughly 6 minutes.

## Your job

### Step 0 — establish the baseline before changing anything

Record what the deployment looks like now, so you can tell your changes apart
from what was already true:

- `fsc scrub coverage --camera stairway-tight` and a couple of others.
- Current `worst live-edge lag` and cycle duration from the sidecar's own
  per-cycle log line (`scrub: cycle ...`). Grab at least 10 minutes of them.
- Free space on the `scrub.cache_dir` filesystem, and the cache's current size.
- The deployed sidecar's git revision, and its effective config (which knobs are
  set explicitly vs defaulted).

### Step 1 — confirm the reported failure is real, before fixing it

**Do this before deploying**, because after the fix the evidence changes.

Fetch `1785816900-60.0-45.jpg` for `stairway-tight` from the cache directory (or
over `/v1`), crop cell 39 at pixel origin (960, 540), and report its mean
brightness. Padding reads ~0; real imagery reads in the hundreds. Also count how
many other cells in that sheet are black, and check whether the index still
declares them.

If the cell is *not* black, say so plainly and stop to reconsider — the whole
diagnosis rests on it, and it was never confirmed against this deployment.

### Step 2 — deploy

Pull `main`, install, restart the sidecar service. Confirm it comes up and the
generation loop is running (the per-cycle log line appears). Note the revision.

### Step 3 — repair the existing index

`fsc scrub verify` first (read-only) — report `overclaiming_sheets` and
`cells_falsely_claimed`, and how the verdicts split between `source: "cells"`
and `source: "pixels"`. Then `fsc scrub verify --repair`, then verify again
expecting zero.

Take the time it actually takes; the ~6 minute figure is unconfirmed here.

This is a **destructive** operation: it deletes sheet rows and files that claim
more than they can render. Back up the sidecar DB first
(`sqlite3 <db> ".backup <path>"`). If `removed` is large (sheets with *no* real
cells at all, deleted outright rather than republished), stop and report before
continuing — that would mean spans the cache never actually sampled, which is a
different problem from the one this fixes.

### Step 4 — re-verify the reported moment

Re-request the index and sheet covering `1785819272` for `stairway-tight`.
Confirm no indexed cell is black-padded. Report the index entry (URL, count) and
the rendered cell.

### Step 5 — the part that can only be done here: cadence at ten-camera scale

This is the most valuable thing you can do, because every cadence number above
came from a single camera.

The concern is concrete: if the live-edge pass takes longer than
`live_edge_interval_s` on ten cameras, every tick overruns and **backfill never
runs at all**. That is the intended priority (history can wait, the edge cannot)
and it is self-correcting once the edge catches up — but if it persists, history
stops converging, and you need to know that rather than discover it in a week.

Watch at least 30 minutes of cycles and report:

- Actual cycle duration per tick, and how much of it is the live pass vs backfill.
- Worst live-edge lag across the ten cameras, sustained.
- Whether backfill is getting *any* time. If it is starved, raise
  `live_edge_interval_s` until it isn't, and report the value that works.
- Whether the fine tier is actually 1 s per camera or has been raised by
  `match_keyframe_cadence` to the camera's GOP (expected: ~1 s on the Dahua
  units, ~5 s on the three UniFi ones). State it per camera.

Then the acceptance check: pick a moment ~3 minutes old on any camera, confirm
the index declares a real cell for it at the finest tier, and confirm the
rendered pixels are non-black.

### Step 6 — disk

Confirm the sweep is running (`superseded_versions_deleted` in the retention
prune log line) and measure the cache's growth rate per camera-hour against the
~49 MB estimate. The 1 s tier dominates. If growth is materially worse than
estimated, say so with numbers — `sheet_version_grace_s` is the knob.

## Rules

- **Report what you measure, not what this document predicts.** If a number here
  is wrong at this scale, that finding is the point of the exercise. Contradicting
  it is a success, not a failure.
- Back up before anything destructive; `verify --repair` and the sweep both
  delete published files.
- Do not change the sheet URL naming or the `/v1/scrub/{camera}/sheets` response
  shape. A released iOS client parses both. Removing false coverage claims is
  the only sanctioned change to what the index reports.
- The app needs no changes — it already falls back to Frigate's preview-frames
  cache when coverage is honestly absent, and picks up denser sheets on its own.
- If you change code, keep the full suite green (`python -m pytest -q`) and ruff
  clean, and push to `main`.
- If something looks wrong that is outside this brief, report it rather than
  fixing it silently.

## Useful entry points

- `src/frigate_sidecar/scrub/generator.py` — `_publish_sheet_version`,
  `_TierWriter.feed`, `generate_live_edge`, `generate_cycle`,
  `sweep_superseded_versions`, `prune`
- `src/frigate_sidecar/scrub/repair.py` — `verify_sheets`, `repair_sheet`
- `src/frigate_sidecar/server.py` — `_scrub_generation_loop`
- `docs/scrub-cache-and-proxy-spec.md` — §4.3 (sheet index contract, updated),
  §5.4 (cadence, updated)
