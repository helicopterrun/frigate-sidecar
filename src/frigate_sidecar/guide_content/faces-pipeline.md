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

## Using it

**Face crops — [Faces](/faces).** Frigate auto-saves small face crops from
detections. The Faces page is the curation grid for them: review, check the
score histogram, then **promote** the good ones into the Face Library or
discard the rest.

**High-res captures — [Face captures](/faces/captures).** Detection streams
are low resolution; faces from them are often too soft to be useful. The
capture stage watches for a person on a **trigger camera** and grabs
full-resolution frames from a paired **capture camera** (e.g. a
doorbell-height camera aimed at the gate) during the visit. In the last 24
hours **{{stat:faces_captured_24h}}** frames were captured.

The captures page groups by visit for fast review: keep the sharp frontal
shots, discard the rest. Kept captures are the raw material the
[identity clustering](/guide/identities) stage feeds on.

## Configuration

- `face:` — the crop-curation feature; needs the `faces` install extra.
- `face_capture:` — trigger/capture camera pairs are explicit here, so
  captures only happen where you've set them up. The output directory must
  be writable by the sidecar service.

## If it goes wrong

No captures appearing: confirm the trigger camera is actually producing
person events, and check the capture rows' status/detail on the page — HTTP
errors from Frigate's snapshot endpoint show up there.
