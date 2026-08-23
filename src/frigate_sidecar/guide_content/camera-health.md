---
title: Camera & detector health
section: analysis
order: 3
routes: ["/fps-budget", "/placement"]
---

## [FPS budget](/fps-budget)

Your detector (e.g. a Coral) has a fixed inference budget: how many frames
per second it can score. This page compares that budget against what your
cameras collectively demand, with color-banded utilization. If demand
approaches budget, detections start queueing and latency climbs — the fixes
are lower detect FPS, smaller detect streams, or better motion masks
(see [Motion & zones](/guide/motion-and-zones)).

## [Placement](/placement)

A calculator for planning cameras before you mount them: enter focal
length/zoom and it derives the horizontal field of view, then shows how many
pixels an object (person, face, plate) occupies at a given distance. Lens
presets cover common hardware.

Rules of thumb it encodes: identification needs far more pixels than
detection, and doubling distance quarters your pixels. Check a planned
mounting spot here before drilling holes.
