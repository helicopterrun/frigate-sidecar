---
title: Face captures & curation
section: faces
order: 1
routes: ["/faces", "/faces/captures"]
config: ["face", "face_capture"]
---

The face pipeline has three stages; this topic covers the first two —
collecting good face images. The third stage, recognition, is
[Identities](/guide/identities).

## Face crops — [Faces](/faces)

Frigate auto-saves small face crops from detections. The Faces page is the
curation grid for them: review, check the score histogram, then **promote**
the good ones into the Face Library or discard the rest. Configured by the
`face:` section (needs the `faces` install extra).

## High-res captures — [Face captures](/faces/captures)

Detection streams are low resolution; faces from them are often too soft to
be useful. The capture stage (config `face_capture:`) watches for a person
on a **trigger camera** and grabs full-resolution frames from a paired
**capture camera** (e.g. a doorbell-height camera aimed at the gate) during
the visit.

In the last 24 hours **{{stat:faces_captured_24h}}** frames were captured.

The page groups captures by visit for fast review: keep the sharp frontal
shots, discard the rest. Kept captures are the raw material the
[identity clustering](/guide/identities) stage feeds on.

Practical notes:

- The capture output directory must be writable by the sidecar service.
- Trigger/capture camera pairs are explicit in config — captures only happen
  where you've set them up.
