---
title: Motion & zone analysis
section: analysis
order: 2
routes: ["/motion", "/zone-hits"]
---

Two pages for understanding what your cameras are reacting to:

## [Motion](/motion)

Per-camera motion activity per hour, classified by activity level. Use it to
spot noisy cameras (wind-blown foliage, flags, shadows) that burn detector
capacity. Add `?baseline=<date>` to switch into A/B compare mode, which
classifies each hour against the baseline day: noise spike, real activity
spike, quiet drop, or flat. That's the tool for answering "did my motion
mask change actually help?"

## [Zone hits](/zone-hits)

Counts how often each zone is entered, per camera, over a window
(`?days=30`). It also surfaces **mask candidates** — regions with lots of
motion but never a meaningful object, which are usually safe to mask out in
Frigate. Fewer wasted motion regions means the detector spends its budget
where it matters (see [Camera health](/guide/camera-health)).
