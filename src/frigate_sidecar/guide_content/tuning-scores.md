---
title: Tuning detection thresholds
section: analysis
order: 1
routes: ["/score-histogram"]
---

[Scores](/score-histogram) plots the confidence-score distribution for any
camera × label pair and suggests thresholds, with confidence banding. Paired
with [Triage](/triage) labels, it turns threshold setting from guesswork
into a measurement.

The logic: false positives cluster at low scores, real detections at high
scores. Labeling a batch of events TP/FP shows you where the two
populations separate — that's your threshold.

```walkthrough
- Pick the camera + label that's producing junk alerts
- Open Triage filtered to that camera/label and label 30-50 events TP/FP
- Open the Scores page for the same camera/label
- Find where FP density falls off and TP density holds
- Set that value as the label's threshold in Frigate's config for the camera
- Restart Frigate and re-check the feed over the next day or two
```

Re-run the loop seasonally — foliage, light, and camera changes shift score
distributions over time.
