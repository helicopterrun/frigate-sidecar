---
title: Identities
section: faces
order: 2
routes: ["/enrich/clusters"]
config: ["face_enrich"]
---

[Identities](/enrich/clusters) is where face recognition becomes useful. A
background worker (config `face_enrich:`, needs the `enrich` install extra)
watches person events on enrolled cameras, samples recording frames, scores
face quality, computes embeddings, and groups recurring people into
**clusters** — no training, no photo upload.

Right now there are **{{stat:clusters_total}}** clusters,
**{{stat:clusters_named}}** of them named.

## How it becomes recognition

A cluster starts **unknown**. The moment you give it a name, it becomes a
**known person**: future sightings write that name into the Frigate event's
`sub_label` (visible in Frigate, the Elsinore app, and notifications), and
the name is retro-written onto the cluster's past events still in retention.
Unnamed clusters that stop appearing expire on their own after
`cluster_ttl_days`.

```walkthrough
- Wait until a cluster shows several sightings of the same person
- Check the sighting strip is coherent — every thumbnail is the same person
- Evict any wrong sighting with its ✕ button
- Type the person's name and tap "name"
- Watch the toast confirm how many past events were relabeled
- If a second cluster of the same person exists, use "merge" to fold it in
```

## Repair tools

- **✕ on a sighting** — removes it from the cluster and rebuilds the
  cluster's face signature exactly from what remains.
- **merge** — combine two clusters of the same person; the named one
  survives. "Looks like…" hints appear when two clusters are suspiciously
  similar.
- **delete** — dissolve a cluster entirely (events keep their history).

Keep Frigate's own face recognition **disabled** on enrolled cameras — the
sidecar is the sole author of `sub_label`, and two writers would fight.
