---
title: Triage
section: sidecar
order: 3
routes: ["/triage", "/event/{event_id}"]
---

[Triage](/triage) is for reviewing detections and labeling them
**true positive (TP)**, **false positive (FP)**, or **SKIP**. The labels feed
the [threshold tuning](/guide/tuning-scores) workflow — a few labeling
sessions give you the evidence to set per-camera thresholds instead of
guessing.

## Using it

**The list.** Filter by camera, object label, triage state, time window, and
sort order. The header shows running TP/FP/SKIP counts, and a **session**
field lets you tag a batch of labels (e.g. `2026-08-23a`) so you can tell
labeling passes apart later.

**The event page.** Tap any event to open `/event/{id}`:

- Snapshot with zone overlays, plus the event clip.
- One-tap **TP / FP / SKIP** buttons (or clear a label).
- Keyboard navigation walks through your filtered list without going back —
  label, arrow, label, arrow. Reviewing a day takes minutes.

Labels are stored in the sidecar's own database; Frigate's data is never
modified.

## If it goes wrong

An empty list usually means the filters are too narrow (widen the day range)
or, on a dev machine, that there's no Frigate database at all — the page
says so explicitly when that's the case.
