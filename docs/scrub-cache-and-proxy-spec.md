# frigate-sidecar: scrub-cache + Frigate proxy — implementation spec

**Status:** design spec, ready to build against. Written 2026-07-30.
**Audience:** whoever implements the sidecar side of Elsinore's reel.
**Companions (client side, in the Elsinore repo):**
- `writing/sidecar-requirements-2026-07-29.md` — what the reel wants, and the measurement behind each ask. This spec is the server-side answer to it.
- `writing/sidecar-scrub-cache-contract.md` — the earlier endpoint-shape draft (superseded here where they differ).
- `writing/frigate-api-verification-2026-07-29.md` — the live 0.17.2 measurements every number below rests on.

This document turns those requirements into something buildable inside *this* repo, using the conventions already here (FastAPI router registration in `server.py`, pydantic-settings config, the read-only `frigate.db` attach in `db.py`, and the ffmpeg + streaming-proxy patterns already in `routes/wildlife.py`).

---

## 1. What we're building

Two features, one origin.

1. **A scrub-preview cache** that serves **uniform-cadence sprite sheets** — one row-major montage of 320×180 frames at a *declared, fixed* interval (1 fps recent, thinning with age). This is the thing bare Frigate structurally cannot give: its preview cache is current-hour-only, motion-driven, irregular, and emptied at the top of every hour. We own retention, resolution, cadence, and — critically — we make the cadence *predictable*.

2. **A transparent reverse proxy** in front of Frigate, so Elsinore holds **one base URL**. Everything the sidecar doesn't handle itself (`/api/*`, `/vod/*`, `/live/*`, `/preview/*`) is streamed through to Frigate unchanged, with `Range` and auth cookies passed through. The scrub cache and proxied Frigate share an origin and an auth story.

Plus a small set of `/v1` read endpoints that collapse the reel's per-window fan-out (coverage, motion, events) into one call and answer questions bare Frigate answers ambiguously or not at all.

### The one principle everything else serves

> **Predictability over density.** 0.5 fps uniform beats 1 fps irregular. The single largest source of complexity in the reel today is that Frigate's frame cadence is bimodal and lands on no boundary. A declared interval turns frame-time into arithmetic (`cell = round((t − start) / interval)`) and deletes a stack of client code: the snap grid, the frame ticks, the 31-second nearest-frame tolerance, and the staleness badge.

### Non-negotiables (inherited from Elsinore PROJECT_PLAN §6 — not re-litigated)

- **One base URL.** The sidecar fronts Frigate; the app holds a single origin.
- **Optional always.** Every `/v1` endpoint degrades to the bare-Frigate path when `/v1/capabilities` finds nothing. The reel must stay usable against plain Frigate forever.
- **Versioned from endpoint one.** Every new path is under `/v1/`.
- **Honest data.** Frames are re-sampled from real recordings. No interpolation, no invented frames, no upsampling. If a moment has no picture, the response says so — the client now has a first-class state for that.

---

## 2. Architecture at a glance

```
                         ┌─────────────────────────────────────────────┐
   Elsinore (iOS) ──────▶│  frigate-sidecar  (single origin, :5001)     │
   one base URL          │                                              │
                         │  /v1/scrub/…    ── sprite cache (disk)  ◀──┐  │
                         │  /v1/coverage/… ── reads frigate.db RO    │  │
                         │  /v1/reel/…     ── coverage+motion+events │  │
                         │  /v1/motion/…   ── total, zero-filled     │  │
                         │  /v1/capabilities                         │  │
                         │  /v1/highlights/… (Tier 1)                │  │
                         │                                           │  │
                         │  everything else ── reverse proxy ────────┼──┼──▶ Frigate :8971
                         │  (/api,/vod,/live,/preview)   Range+cookie │  │   (authed)
                         └───────────────────────────────────────────┼──┘
                                                                      │
   generator (background)  ── enumerate segments from frigate.db ─────┘
       every ~60s          ── ffmpeg -vf fps=1/N,scale=320:180 ──▶ tile ──▶ sheet.webp on disk
       extend toward now   ── record bucket/sheet rows in sidecar.db
                           ── read segments from Frigate's /media (RO mount)
```

Three moving parts:

- **The generator** (§5): a continuous background loop that samples real recording segments to a uniform cadence, tiles them into sheets on disk, and records what it produced in the sidecar DB.
- **The `/v1` read layer** (§4): serves sheets and coverage from disk + `frigate.db`, immutable and ETag'd.
- **The proxy** (§6): forwards everything else to Frigate so the app has one origin.

---

## 3. Two decisions that shape everything — recommended, flagged for sign-off

These two are genuinely the deployment owner's call and they change the build. Recommendations given; see §12 for the rest.

### 3.1 Generation source — **recommend: recording segments via a `/media` read-only mount**

There are two possible frame sources:

| Source | Uniform cadence? | Top-of-hour hole? | Needs `/media` mount? |
|---|---|---|---|
| **Recording segments** (`.mp4` on `/media`, sampled with `ffmpeg -vf fps=1`) | **Yes — ffmpeg guarantees it** | **No** — recordings are continuous 10 s segments | Yes (RO) |
| Preview cache over HTTP (`preview.mp4` + preview WebP frames) | No — motion-driven, irregular; resampling to "uniform" would fabricate cadence | **Yes** — `preview.mp4` 404s until the hour completes; WebP cache emptied at :00 | No |

The requirements ask for uniform cadence *and* name the top-of-hour hole as the worst thing about Frigate's own cache. **Only the recordings path satisfies both honestly** — `ffmpeg -vf fps=1` samples real frames at exact 1 s spacing, and recordings have no hourly hole. This also makes `published_through` and `recorded` free: they come straight from `frigate.db`'s `recordings` table, which the sidecar already opens read-only.

The cost is a read-only bind-mount of Frigate's recordings directory (the sidecar already bind-mounts `config.yml` and `frigate.db`; this is one more line — see §8). Where that's genuinely impossible, §5.6 gives an HTTP-only degraded generator, but it cannot promise a clean interval and should be treated as a fallback, not the design.

**This spec assumes the recordings-via-`/media` path throughout.**

### 3.2 Proxy target & auth — **recommend: proxy to Frigate's authed port (:8971), pass the app's cookie through**

Today `FrigateClient` talks to `frigate.base_url`, which points at Frigate's *unauthenticated* internal API (`:5000`). That's correct for the sidecar's own server-to-server analysis calls and must stay.

For **proxying app traffic**, the sidecar should forward to Frigate's **authenticated** endpoint (`:8971`) and pass the client's `Authorization`/`Cookie` headers through untouched. This keeps auth exactly where it is — Frigate's — so:
- Elsinore reuses `FrigateSession.playbackCookieContext()` unchanged; there's no second password story.
- The sidecar never holds or validates the user's Frigate password.
- `/vod/*` and `/live/*` playback carry the same session cookie they already do.

Config therefore grows a **second** Frigate URL: `frigate.proxy_base_url` (the authed `:8971`) distinct from `frigate.base_url` (the internal `:5000`). See §7.

**Protecting the `/v1` endpoints themselves:** the sprite cache is low-sensitivity (320×180 preview stills, same footage the proxied `/preview` already exposes). Two viable options:
- **(a)** Require the same Frigate auth cookie on `/v1/*` and validate it by a cheap upstream call (proxy a `HEAD /api/…` and mirror 401). Uniform with the rest.
- **(b)** Trust the LAN / the existing NPM reverse-proxy layer that already fronts this host (per the current deployment, Nginx Proxy Manager terminates TLS and can gate access).

Recommend **(b) for v1** (simplest, matches how the sidecar's other pages are already exposed) with a note that **(a)** is the clean answer once off-LAN relay (Elsinore §12.4) is real. Flagged in §12.

---

## 4. `/v1` endpoint contracts

All timestamps are **unix seconds, floating point** — never ms, never strings, never ISO-8601 (matches Frigate and the client's `TimeInterval`). All new routes live under `/v1/`. New router module: `src/frigate_sidecar/routes/scrub.py`, registered in `server.py` alongside the others.

### 4.0 Contract hygiene (applies to every endpoint below)

Each of these cost a debugging session on bare Frigate; bake them in from the start.

- **Never 200 an unknown path.** Return `404` with a JSON body, never HTML. (Frigate's SPA catch-all 200s any non-`/api` path with HTML; a typo then fails at the decoder. FastAPI won't do this for `/v1`, but the proxy must not forward SPA-shell 200s as if they were data — see §6.4.)
- **Errors carry a machine-readable reason:** `{"error": "not_generated", "message": "…"}`. The reel draws "nothing here yet" differently from "something is broken" and cannot without being told. Reason vocabulary: `not_generated`, `not_covered`, `camera_unknown`, `upstream_unavailable`, `bad_range`.
- **`Cache-Control: public, max-age=31536000, immutable`** on any sheet or frame older than the bucket's `generated_through`. They never change; `URLSession`'s own cache then does the LRU work and the client deletes its bounded cache. (Same header `wildlife.py` already puts on content-addressed posters.)
- **`ETag`** on `/v1/coverage` and `/v1/reel` (they're polled while the reel is open). A matching `If-None-Match` returns `304`.
- **Times float, always.**

### 4.1 Capability probe — `GET /v1/capabilities`

```
200 → {
  "version": "1.0.0",
  "scrub_cache": {
    "enabled": true,
    "format": "sprites",              // "sprites" | "jpegs" — which reader the client uses
    "cameras": ["doorbell", "garden"], // cameras actually being generated (see §12 Q3)
    "generated": true                  // false = running but hasn't backfilled yet
  },
  "proxy":  { "enabled": true },
  "push":   { "enabled": false },
  "http2":  true                       // is this origin reachable over HTTP/2 (§6.5)
}
```

- A `404`, or `scrub_cache.enabled == false`, means **fall back to bare Frigate**.
- `cameras` lists what is *generated*, not merely what's enabled — a sidecar that's up but hasn't backfilled a given camera is a different state from one that has, and the reel shows the difference rather than looking broken.
- `format` lets JPEG-first ship before sheets exist without a contract change (§5.4).

### 4.2 Scrub coverage (what sprite data exists) — `GET /v1/scrub/{camera}/coverage?start={ts}&end={ts}`

This answers *"what frames do I have, and at what cadence"* — distinct from recording coverage (§4.4), which answers *"what did Frigate record"*.

```
200 → {
  "camera": "doorbell",
  "buckets": [
    { "start": 1785380400, "end": 1785384000, "interval": 1.0, "width": 320, "height": 180 },
    { "start": 1785294000, "end": 1785380400, "interval": 5.0, "width": 320, "height": 180 }
  ],
  "generated_through": 1785384060,
  "retention_days": 14
}
```

- **`interval` is a hard contract:** every frame in `[start, end)` exists at `start + n·interval`. If one is genuinely missing (a recording gap), **split the bucket** rather than fudge the interval. The client relies on this to place frames by arithmetic with no per-frame list.
- **`generated_through`** is the newest moment with a frame behind it — the sidecar's own live-edge lag, stated so the reel never guesses.
- Buckets may **thin with age** (`interval` 1.0 recent, 5.0 older). Expected; the reel draws it honestly.
- **No `frames[]` array anywhere.** The uniform interval makes it redundant, and a redundant array is one that can disagree with the image.

### 4.3 Sprite sheets — `GET /v1/scrub/{camera}/sheets?start={ts}&end={ts}` and the images

```
GET /v1/scrub/{camera}/sheets?start={ts}&end={ts}
200 → {
  "sheets": [
    {
      "url": "/v1/scrub/doorbell/sheet/1785380400-1.0.webp",
      "start": 1785380400, "interval": 1.0,
      "cols": 12, "rows": 8, "cell_w": 320, "cell_h": 180, "count": 96
    }
  ]
}

GET /v1/scrub/{camera}/sheet/{start}-{interval}.webp   → image/webp (or image/jpeg), immutable
```

- **Cell for time `t`:** `idx = round((t − start) / interval)`, laid out **row-major** — `row = idx // cols`, `col = idx % cols`. No timestamp list; nothing to get out of sync.
- **Why sheets, not individual frames:** measured, individual frame fetches saturate at ~88 frames/s over six HTTP/1.1 connections and going wider *worsens* per-request latency (p95 110 ms → 250 ms from 6→24 conns) without the client being able to use the throughput. One sheet fetch covering a whole drag span sidesteps the connection cap entirely.
- **Sizing:** cap a sheet at ~**96 cells** (≈ two minutes of 1 fps timeline). At 320×180 that's a decoded footprint of ~20 MB; the client holds two or three. `count` may be < `cols·rows` for the newest (still-filling) sheet.
- The `sheet/{start}-{interval}.webp` filename is content-addressed by (bucket start, interval), so it's immutable once complete → the immutable cache header applies and the client's own URL cache serves repeats.

### 4.4 Recording coverage (what Frigate recorded) — `GET /v1/coverage/{camera}?start={ts}&end={ts}`

The field that would have prevented four bugs this week. Read straight from `frigate.db`'s `recordings` table (RO) — no proxy call, no parsing hundreds of segment objects on the client.

```
200 → {
  "camera": "doorbell",
  "queried":  [1785380000, 1785384000],
  "recorded": [[1785380000, 1785381240], [1785381600, 1785383990]],
  "published_through": 1785383990,
  "retention_days": 30
}
```

- **`queried`** is the span this answer covers. Anything outside it is **unknown, not empty**. This one field is the whole ask: it lets the reel stop conflating "no recording" with "I haven't looked". *(Bare Frigate's empty array means both.)*
- **`recorded`** as merged intervals, not raw segments — the reel already merges them; doing it server-side saves parsing and removes segment-boundary off-by-ones.
- **`published_through`** is where the recorder has actually committed = `MAX(end_time)` over the camera's `recordings` rows. Measured: the newest segment trails real time by **4–10 s** because a segment is published only once complete. The reel currently hard-codes a 20 s "don't make claims" band; with this it uses the real number.
- **`ETag`** required (§4.0): the window containing *now* is re-polled every ~10 s; a `304` lets the sidecar decide how often the answer really changes.

### 4.5 One call per reel window — `GET /v1/reel/{camera}?start={ts}&end={ts}&motion_scale={s}`

Collapses the three-or-four parallel requests the reel makes to paint one window (motion, segments, events, frame list) into one. Same lifetime, same cache key, same failure mode → one call.

```
200 → {
  "queried":  [start, end],
  "recorded": [[…]],
  "published_through": 1785383990,
  "frames": { "start": …, "interval": 1.0, "count": 3600 },      // descriptor, not data — mirrors scrub coverage
  "motion": { "start": …, "interval": 10, "values": [0,4,61,88,…] },
  "events": [ { "id": "…", "label": "person", "zones": ["driveway"],
               "start": 1785381200, "end": 1785381260, "score": 0.81 } ]
}
```

- **`motion.values` is a bare array on a declared grid** — `value[i]` covers `[start + i·interval, start + (i+1)·interval)`. An hour at `scale=10` is 360 numbers, not 360 timestamped objects. Biggest byte win on the page and it costs nothing to produce.
- `frames` is the same descriptor as §4.2 (start/interval/count), so the client places cells by arithmetic.
- Deletes client-side: three window caches, three in-flight guards, and the `ReelDataSource` latest-wins pump that exists only because those three sources could disagree about which window they described.
- **ETag** as §4.0.

### 4.6 Total motion — `GET /v1/motion/{camera}?start={ts}&end={ts}&scale={s}`

Re-serve Frigate's `/api/review/activity/motion` (which is genuinely good — 61 ms, normalised 0–100 per camera, resolves to 1 s) but fix its two measured cliffs:

- **`scale=3600` returns all zeros** over multi-day windows while claiming full coverage → the client caps `scale` at 300 and aggregates the rest itself.
- **Short windows return short answers** — a 1800 s request at `scale=60` came back with 10 buckets covering 540 s.

Contract: **any `scale`, always covering the full requested `[start, end)`, zero-filled where there is genuinely no data.** The sidecar fetches from Frigate at a safe scale (≤300), aggregates/zero-fills to the requested grid, and returns the bare-array form. Then the client's `ReelGranularity.motionScale` and `aggregatesMotion` both disappear. (This is also the `motion` block inside §4.5 — implement once, expose both.)

### 4.7 Highlights (Tier 1, after Tier 0) — `GET /v1/highlights/{camera}?before={ts}&limit=10`

Turns the reel from a ruler into a search tool — "take me to the next interesting thing", which needs a ranked index across a long span that no Frigate endpoint provides.

```
200 → { "highlights": [ { "start": …, "end": …, "reason": "person", "score": 0.9 } ] }
```

Cheap for the sidecar: precompute from `review` items + motion. This is the one item that adds a *feature* rather than removing client complexity — build it after Tier 0, before the rest of Tier 1.

### 4.8 On-release handoff (no new endpoint)

Sprites drive the *drag*; on release the player seeks real video via the VOD range endpoint `/vod/{camera}/start/{start}/end/{end}/index.m3u8` — which is **already reachable at this same origin through the proxy** (§6), so there's no second auth story. Nothing to build here; it's a consequence of the proxy existing.

---

## 5. The generator

The heart of feature 1. A continuous loop that keeps the sprite cache extended toward *now* and thinned with age.

### 5.1 Source of truth: `frigate.db` + `/media`

The sidecar already opens `frigate.db` read-only (`db.py::open_frigate_ro`). Frigate's `recordings` table holds one row per ~10 s segment with `camera`, `path`, `start_time`, `end_time`, `duration` (verify exact columns against the live 0.17.2 schema at runtime — reuse the `PRAGMA table_info` pattern already in `triage/sampler.py::_select_optional_columns`). This gives, for free and without a proxy round-trip:

- the **segment files to sample** (their `path`, mapped from Frigate's container path to the sidecar's mount — see §8.2),
- **`recorded`** intervals and **`published_through`** for §4.4 (`MAX(end_time)`),
- exactly which spans have footage, so the generator never wastes ffmpeg on a gap.

### 5.2 Sampling to a uniform interval

For a target bucket `[t0, t1)` at `interval = N` seconds, per contributing segment:

```
ffmpeg -nostdin -loglevel error -ss <offset> -i <segment.mp4> \
       -vf "fps=1/N,scale=320:180" -q:v 4 -f image2 <outdir>/%06d.jpg
```

- `-vf fps=1/N` resamples to exactly one frame per N seconds — **this is what makes the cadence uniform and honest**: it selects the real frame nearest each grid point, never interpolates.
- `scale=320:180` — §1.5 of the requirements: 320×180 is enough; don't spend disk on resolution. Spend budget on interval and retention instead.
- Frame `k` from segment start maps to wall-clock `segment.start_time + k·N`, which maps to sheet cell `round((t − bucket.start) / N)`.
- Reuse the concurrency discipline already in `wildlife.py`: an `asyncio.Semaphore` (start at 3) caps simultaneous ffmpeg processes, and a per-extraction timeout kills a wedged one.

### 5.3 Tiling into sheets

Accumulate frames into a montage of `cols × rows` (target ~96 cells, e.g. 12×8). Two viable tiling paths:

- **ffmpeg `tile` filter** in one pass: `-vf "fps=1/N,scale=320:180,tile=12x8" ` writes montage(s) directly — fewest processes, no intermediate JPEGs.
- **Pillow/`opencv`** compositing from the per-frame JPEGs — more control over partial (still-filling) sheets and re-encoding to WebP.

Recommend the **ffmpeg `tile`** path for completed buckets (cheap, one process) and Pillow only for the **live, partially-filled** sheet that's re-tiled each cycle until full. Output **WebP** (13.6 KB/frame measured on Frigate's own WebP; comparable here) with **JPEG as the shipping fallback** — `capabilities.scrub_cache.format` tells the client which, so JPEG-first can ship before WebP tiling lands with zero contract change.

Write atomically: extract to a temp file in the cache dir, then `os.replace` into place (the exact atomic-publish pattern in `wildlife.py::wildlife_poster`). A sheet is only advertised in `/v1/scrub/.../sheets` once fully written.

### 5.4 Cadence: **continuous, ~60 s timer — never hourly**

⚠️ **This is the single most important operational rule.** The earlier contract draft proposed an hourly cron. **That reproduces the worst hole we have.** Measured at 19:01:40, Frigate's own WebP cache held exactly three frames because it's emptied at the top of every hour, and `preview.mp4` for the current hour 404s until the hour completes — so the first stretch of every hour, which is *exactly the recent past people look at*, is nearly empty. An hourly cron would rebuild that hole in the sidecar.

Instead: a background loop on a **short timer (~60 s)** that always extends the newest bucket toward `published_through`. A minute of live-edge lag is invisible; an hour of it is the bug we already have. Publish `generated_through` so the client knows where the edge is.

Two implementation shapes:
- **(a) In-process asyncio task** started in the FastAPI lifespan, looping every 60 s. Survives as long as uvicorn does; simplest; shares the process. **Recommended.**
- **(b) systemd timer** running `fsc scrub generate` every 60 s (mirrors the existing `contrib/frigate-sidecar-faces.timer`). Better isolation; survives a wedged event loop; matches the LXC deployment's habits.

Recommend **(a)** for the continuous forward edge, with the CLI (§5.7) also available for **(b)** and for one-shot backfill. Either way it is *not* hourly.

### 5.5 Retention & thinning

- Keep a **recent tier at 1 fps** and **thin older spans** to a coarser interval (e.g. 5 s past 24 h) by generating those buckets at the coarser `interval`. The bucket schema already carries `interval`, so the reel draws the thinning honestly.
- A prune pass (part of the loop, or `fsc scrub prune`) drops sheets whose bucket `end < now − retention_days`, oldest-first — the bounded-by-mtime eviction `wildlife.py::_prune_poster_cache` already demonstrates.
- **Disk math (from the requirements, for sign-off in §12):** 1 fps · 320×180 · sprite-JPEG ≈ **0.4–0.5 GB/camera/day**; 8 cameras × 14 days ≈ **50–60 GB**. If that's too much, make the recent tier 1 fps and everything past 24 h drop to 0.2 fps — the buckets say which, so the reel needs no change.

### 5.6 Degraded HTTP-only generator (fallback only)

If a `/media` mount is truly impossible (§3.1), the generator can instead pull `preview.mp4` in **≤4-minute windows** (measured: the response caps at ~254 frames, so wider requests just thin the same frames) and, for the current hour, the individual preview WebP frames. **But this cannot promise a clean interval** — the source is motion-driven — so the buckets it produces must declare the *actual* achieved spacing and accept that "uniform" degrades to "as-uniform-as-the-source". Treat as a compatibility path, not the design.

### 5.7 CLI surface (mirrors the existing `fsc` groups)

Add a `scrub` typer group in `cli.py`, matching how `triage`/`analysis`/`faces` are structured:

```
fsc scrub generate  --camera doorbell [--since <ts>] [--interval 1]   # forward edge / one cycle
fsc scrub backfill  --camera doorbell --days 14 [--interval 1]        # one-time history fill
fsc scrub prune     [--camera doorbell]                               # apply retention
fsc scrub coverage  --camera doorbell                                 # print what's generated (debug)
```

Backfill choice (§12 Q4): batch the whole retention window on first run, or generate forward-only and let history fill in? Affects whether the reel shows a "generating" state — `capabilities.scrub_cache.generated` exists for exactly this.

---

## 6. The reverse proxy

Feature 2: make the sidecar the single origin. New router `routes/proxy.py`, registered **last** so `/v1/*`, `/static`, the sidecar's own HTML pages, and `/healthz` win first; everything else falls through to Frigate.

### 6.1 What it forwards

A catch-all that streams to `frigate.proxy_base_url` (the authed `:8971`, §3.2): `/api/*`, `/vod/*`, `/live/*` (go2rtc MSE/WebRTC), `/preview/*`, and Frigate's own assets. The reel only *needs* `/api/*` and `/vod/*` proxied today, but a transparent catch-all is simpler and future-proofs live streaming.

### 6.2 How — reuse the wildlife proxy pattern verbatim

`routes/wildlife.py` already implements a correct streaming reverse proxy; the scrub proxy is the same shape, generalised:

- **`httpx.AsyncClient` with `stream=True`**, body relayed via `StreamingResponse` (`wildlife.py::wildlife_media`).
- **Forward request headers** `Range` **and** `Authorization`/`Cookie` (extend the `_REQ_PASS` allow-list — wildlife only needed `range` because its upstream is unauthenticated; here we must also pass auth through so it stays Frigate's).
- **Relay response headers** `content-type`, `content-length`, `content-range`, `accept-ranges`, `cache-control`, **`etag`**, **`set-cookie`**, **`www-authenticate`** (extend `_RESP_PASS`).
- **Mirror upstream status** — 200/206/**401**/404 all pass through unchanged, so Frigate's auth challenge reaches the client intact.
- **No read timeout on media** (`httpx.Timeout(connect=…, read=None)`) — VOD/live are long-lived streams the user pauses and seeks (`wildlife.py` already does this).
- **Traversal guard** on the captured path (`".." in path.split("/")`), as wildlife does.
- Method pass-through: the reel is read-only (GET/HEAD), but `POST /api/reviews/viewed` and the export endpoints (Elsinore Tier 3) want POST/PUT — forward the method and body generically rather than GET-only.

### 6.3 What NOT to reimplement

Front these, don't rebuild them — they're already fast on bare Frigate: VOD manifests (146 ms, ~12 KB/hr), `/api/config`, `/api/stats`, snapshots, thumbnails. The proxy exists to unify the *origin*, not to re-serve what Frigate does well.

### 6.4 The SPA-catch-all trap

Frigate 200s any non-`/api` path with the web UI's HTML shell. The proxy must not let that masquerade as data: because we forward transparently and the *client* decodes, this is mostly the client's concern — but the sidecar should still **never** synthesize its own 200-with-HTML for an unknown `/v1` path (FastAPI returns JSON 404 by default; keep it that way, §4.0).

### 6.5 HTTP/2 — the cheapest item on the page

Measured: Frigate speaks **HTTP/1.1**, so `URLSession` caps at 6 connections/host and per-request latency *doubles* between 6 and 24 connections; prefetch is currently a latency trick, useless for throughput. If the sidecar's front (uvicorn, or the **NPM/Nginx layer already terminating TLS on this host**) speaks **HTTP/2**, multiplexing lets sheet fetches, coverage polls, and frame fetches stop queueing behind each other, and prefetch becomes real read-ahead. **This is a reverse-proxy config line, not a feature** — enable HTTP/2 on whatever fronts `:5001`. `capabilities.http2` advertises it so the client knows whether to widen its prefetch. Likely the highest value-per-effort item in this whole spec.

---

## 7. Config additions (`config.py`)

Extend the pydantic-settings models. New sections; existing ones untouched. Env overrides follow the established `FRIGATE_SIDECAR_…__…` convention.

```python
class FrigateSection(BaseModel):
    base_url: str = "http://frigate.lan:5000"          # (existing) internal, unauth — sidecar's own calls
    proxy_base_url: str = "http://frigate.lan:8971"    # NEW: authed origin for app-traffic proxy (§3.2)
    config_path: Path = Path("/opt/frigate/config.yml")
    db_path: Path = Path("/opt/frigate/database/frigate.db")
    media_path: Path = Path("/media/frigate")          # NEW: RO mount of Frigate recordings (§8.2)

class ScrubSection(BaseModel):
    enabled: bool = False                              # off by default; opt-in per deployment
    cameras: list[str] = []                            # [] = all cameras; else the opt-in set (§12 Q3)
    cache_dir: Path = Path("/data/scrub")              # sheets live alongside the sidecar DB
    recent_interval_s: float = 1.0                     # 1 fps recent tier
    aged_interval_s: float = 5.0                       # thinned tier
    aged_after_h: float = 24.0                         # when to drop to the aged interval
    retention_days: int = 14                           # ours to set, independent of record.retain.days
    cell_w: int = 320
    cell_h: int = 180
    sheet_cols: int = 12
    sheet_rows: int = 8                                # 96 cells ≈ 2 min at 1 fps
    format: str = "webp"                               # "webp" | "jpeg" (§5.3)
    generate_interval_s: float = 60.0                  # the ~60 s continuous edge (NOT hourly) (§5.4)
    ffmpeg_concurrency: int = 3                         # semaphore width (matches wildlife)

class ProxySection(BaseModel):
    enabled: bool = True
    pass_request_headers: list[str] = ["range", "authorization", "cookie"]
```

Add `scrub: ScrubSection` and `proxy: ProxySection` to `Settings`, and update `config/sidecar.example.yml` with commented defaults (following the existing heavily-commented style).

---

## 8. Storage & DB

### 8.1 Sidecar DB schema — extend `SIDECAR_SCHEMA` in `db.py`

Two tables, appended to the existing `executescript` block (same style as `triage_labels`, `face_attempts`, `toybox_scores`):

```sql
CREATE TABLE IF NOT EXISTS scrub_buckets (
    camera            TEXT NOT NULL,
    start_ts          REAL NOT NULL,        -- inclusive
    end_ts            REAL NOT NULL,        -- exclusive; grows as the live bucket fills
    interval_s        REAL NOT NULL,        -- the hard-contract cadence
    width             INTEGER NOT NULL,
    height            INTEGER NOT NULL,
    generated_through REAL NOT NULL,        -- newest moment with a frame behind it
    complete          INTEGER NOT NULL DEFAULT 0,  -- 1 once end_ts is final & immutable
    PRIMARY KEY (camera, start_ts, interval_s)
);
CREATE INDEX IF NOT EXISTS idx_scrub_bucket_cam ON scrub_buckets(camera, start_ts);

CREATE TABLE IF NOT EXISTS scrub_sheets (
    camera    TEXT NOT NULL,
    start_ts  REAL NOT NULL,                -- first cell's wall-clock; also the filename key
    interval_s REAL NOT NULL,
    cols      INTEGER NOT NULL,
    rows      INTEGER NOT NULL,
    cell_w    INTEGER NOT NULL,
    cell_h    INTEGER NOT NULL,
    count     INTEGER NOT NULL,             -- filled cells (< cols*rows for the live sheet)
    path      TEXT NOT NULL,                -- on-disk relative path under scrub.cache_dir
    complete  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera, start_ts, interval_s)
);
CREATE INDEX IF NOT EXISTS idx_scrub_sheet_cam ON scrub_sheets(camera, start_ts);
```

`recorded` / `published_through` for §4.4 are **not** stored here — they're read live from `frigate.db`'s `recordings` table so they never drift from reality.

### 8.2 On-disk layout & path mapping

```
{scrub.cache_dir}/{camera}/{interval}/{bucket_start}.webp
```

Content-addressed by (camera, interval, bucket start) → immutable once complete → the immutable cache header applies and the sheet URL is stable forever.

**Path mapping:** `recordings.path` in `frigate.db` is Frigate's *container* path (e.g. `/media/frigate/recordings/2026-07-30/…`). The sidecar sees that tree at `frigate.media_path`. If the two roots differ, map by replacing Frigate's recordings root prefix with `frigate.media_path`. Verify against the live deployment (the faces config already hard-codes a host-side media path — `/mnt/frigate-storage/recordings/...` — so the mapping is deployment-specific and belongs in config).

---

## 9. Deployment changes

### 9.1 `/media` read-only mount

`docker-compose.yml` gains one volume (RO), matching the existing `config.yml`/`frigate.db` mounts:

```yaml
    volumes:
      - /opt/frigate/config.yml:/opt/frigate/config.yml:ro
      - /opt/frigate/database/frigate.db:/opt/frigate/database/frigate.db:ro
      - /media/frigate:/media/frigate:ro          # NEW — recordings, read-only
      - /opt/frigate-sidecar/data:/data           # sheets live here (scrub.cache_dir=/data/scrub)
      - ./config/sidecar.yml:/etc/frigate-sidecar/sidecar.yml:ro
```

For the **systemd/LXC deployment** (the one actually in use — an editable install restarted via `systemctl restart frigate-sidecar.service`), the recordings path is a host path already present; just point `frigate.media_path` at it. No mount needed.

### 9.2 ffmpeg must be present

`wildlife.py` already shells out to `ffmpeg`, and the generator depends on it. The current `Dockerfile` (`python:3.12-slim`) does **not** install it — so the Docker path needs `RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg`. The systemd/LXC deployment relies on host ffmpeg (already true for wildlife posters). Flag this as a real gap for the Docker image regardless of this spec.

### 9.3 Generator process

If §5.4(a) in-process: nothing new to deploy — the FastAPI lifespan starts the loop. If §5.4(b) systemd timer: add `contrib/frigate-sidecar-scrub.timer` + `.service`, cloning `contrib/frigate-sidecar-faces.{timer,service}`, running `fsc scrub generate` every 60 s.

### 9.4 HTTP/2 at the edge

Enable HTTP/2 on whatever fronts `:5001` (the existing NPM/Nginx TLS terminator is the natural place — §6.5). One config line; advertise via `capabilities.http2`.

---

## 10. Fallback / degradation (the "optional always" guarantee)

Every capability degrades cleanly:

| Sidecar state | Reel behaviour |
|---|---|
| `/v1/capabilities` 404 or unreachable | Full bare-Frigate path: preview WebP frames (current hour) + `preview.mp4` (older), motion via `/api/review/activity/motion`, coverage via `/api/{cam}/recordings`. The reel already does this today. |
| `scrub_cache.enabled=false` | Proxy + `/v1/coverage`/`/v1/reel` still help; frames fall back to the two-source model. |
| Camera not in `scrub_cache.cameras` | That camera uses the bare-Frigate frame path; others use sheets. |
| `scrub_cache.generated=false` (backfilling) | Reel shows a "generating" state for spans without buckets rather than "no data". |
| Proxy disabled | App keeps talking to Frigate directly for `/api`,`/vod` (two origins), `/v1` still available if reachable. |

The reel must never *require* a `/v1` endpoint to be usable. This is the load-bearing constraint behind versioning from endpoint one and the capability probe.

---

## 11. Testing

Follow the existing `tests/` conventions (`test_api.py`, `test_sampler.py`, `test_recorder.py`, `conftest.py` fixtures; `pytest` + `pytest-asyncio`, already configured).

- **`test_scrub_generate`** — given a fixture segment (a tiny checked-in mp4) and a fake `recordings` row, assert the generator writes a sheet with the declared `count`, that cell `k` is the frame at `start + k·interval`, and that a gap **splits the bucket** rather than fudging the interval.
- **`test_scrub_coverage`** — bucket rows → coverage JSON: interval contract holds, `generated_through` is the true edge, aged buckets carry the coarser interval.
- **`test_reel_bundle`** — one call returns `queried`/`recorded`/`published_through`/`frames`/`motion`/`events`; `motion.values` is a bare array on the declared grid, zero-filled to the full requested range; `queried` ≠ `recorded` when the window overhangs available footage.
- **`test_motion_totalizes`** — a short-window / coarse-scale upstream response is aggregated & zero-filled to cover the *full* requested range (the two measured cliffs).
- **`test_proxy_passthrough`** — `Range` and `Authorization` forwarded; `206`/`401`/`404` mirrored; `..` traversal rejected; `set-cookie`/`etag` relayed. (Mock the upstream with `httpx` transport, as the wildlife proxy tests would.)
- **Contract-hygiene asserts** — unknown `/v1` path → JSON 404 (never HTML 200); immutable header on completed sheets; ETag → 304 on `If-None-Match`; all times float.
- **Reuse the Elsinore JSON fixtures** (`FrigateKit/Tests/.../Fixtures/*.json`: `preview_frames_sample.json`, `activity_motion_sample.json`, `events_window_sample.json`) as golden inputs so the two sides agree on shape.

---

## 12. Open decisions — need sign-off before/while building

The first two (from §3) change the build shape; the rest are the requirements doc's open questions, carried here so they're decided deliberately.

1. **Generation source** — recordings via `/media` RO mount (recommended: uniform + no hourly hole) vs HTTP-only preview (no mount, but can't promise clean cadence). §3.1.
2. **Proxy target & `/v1` auth** — proxy to authed `:8971` with cookie pass-through (recommended); protect `/v1` via the existing NPM/LAN layer for v1, upstream-validated cookie later. §3.2.
3. **Disk budget** — accept ~50–60 GB (8 cams × 14 d × 1 fps), or make recent-tier 1 fps and everything past 24 h drop to 0.2 fps? Either is drawable honestly; the buckets declare which. §5.5.
4. **Retention independent of `record.retain.days`?** The point of owning the cache is that we set this. Decide up front — it changes nothing in the schema (there's a `retention_days`), but confirms whether it must ever differ per camera.
5. **All cameras or a chosen set?** Eight cameras of always-on ffmpeg is real CPU. `scrub.cameras` + `capabilities.scrub_cache.cameras` support a per-camera opt-in; decide the default. §7.
6. **Backfill on first run** — batch the whole retention window, or forward-only + fill history lazily? Drives whether the reel shows "generating". §5.7.
7. **Sheet span / cell size** — 96 cells (12×8) at 320×180 ≈ 2 min/sheet, ~20 MB decoded, hold 2–3. Confirm or tune. §4.3.

---

## 13. Suggested build order

Tier 0 first — it's what changes the app. Each step is independently shippable and degrades cleanly if the next never lands.

1. **Proxy** (`routes/proxy.py`) + `frigate.proxy_base_url` — one origin. Cheapest, unblocks the on-release VOD handoff immediately, and is a near-copy of the wildlife proxy. Turn on **HTTP/2** at the edge in the same step (§6.5).
2. **`/v1/capabilities`** + **`/v1/coverage`** (reads `frigate.db`, no generation yet) — kills the four "no recording vs not looked" bugs on their own, before any sprite exists.
3. **Generator → JPEG sheets** (`routes/scrub.py`, `scrub` CLI, DB tables, continuous 60 s loop) + **`/v1/scrub/{camera}/coverage`** and `/sheets` — the uniform-cadence cache. Ship JPEG (`format:"jpeg"`); the contract doesn't change when WebP tiling arrives.
4. **`/v1/reel`** + **`/v1/motion`** — collapse the fan-out, totalize motion.
5. **WebP tiling** (flip `format`), **retention/thinning**, **prune**.
6. **`/v1/highlights`** (Tier 1 feature).

Tier 2 (push over MQTT+APNs, a unified `/ws` change feed, QR pairing, off-LAN relay) is already owned elsewhere in Elsinore PROJECT_PLAN §6 and out of scope here — but the proxy and capability probe built above are its foundation.
