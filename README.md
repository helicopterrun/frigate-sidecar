# frigate-sidecar

A small companion server for [Frigate NVR](https://github.com/blakeblackshear/frigate).
It adds:

- **Triage UI** for labeling tracked-object events as true-positive / false-positive
  / skip, with snapshots, video clips, and zone overlays. Labels go into a
  sidecar SQLite DB; Frigate's own DB is opened read-only.
- **Read-only analysis** over Frigate's database (score histograms, motion rate,
  FPS budget vs detector capacity, zone hit counts, annotation offset
  diagnostics, etc.) — things Frigate's own UI doesn't expose. Available both
  as a CLI and as JSON HTTP endpoints.
- **Scrub-cache** (`/v1/scrub`) — a uniform-cadence sprite-sheet cache generated
  from real recording segments, so clients get a predictable, gap-free
  timeline instead of Frigate's motion-driven, top-of-hour-emptied preview
  cache. See [`docs/scrub-cache-and-proxy-spec.md`](docs/scrub-cache-and-proxy-spec.md)
  for the full design.
- **Reverse proxy** to Frigate's authenticated origin, so a client can hold a
  single base URL — everything the sidecar doesn't handle itself streams
  through to Frigate unchanged, with `Range` and auth cookies passed through.

Runs as a single Docker container next to Frigate, bind-mounting Frigate's
`config.yml` and `frigate.db` read-only and writing its own SQLite DB.

## Setup

Two supported deployment shapes. The Docker compose file is the canonical
"spin it up" path; the systemd unit is useful when Docker isn't an option
(e.g. running inside an LXC where Docker can't load AppArmor profiles
during image build).

### Option A — Docker compose (recommended)

1. Clone the repo:
   ```sh
   git clone https://github.com/helicopterrun/frigate-sidecar
   cd frigate-sidecar
   ```

2. Copy and edit the example config — point it at your Frigate install:
   ```sh
   cp config/sidecar.example.yml config/sidecar.yml
   $EDITOR config/sidecar.yml
   ```

3. Create the data directory the sidecar will write to:
   ```sh
   sudo mkdir -p /opt/frigate-sidecar/data
   sudo chown -R "$(id -u):$(id -g)" /opt/frigate-sidecar/data
   ```

4. Launch:
   ```sh
   docker compose up -d
   ```

5. Open `http://<host>:5001`.

### Option B — Systemd (host process)

For environments where Docker image builds fail (e.g. unprivileged LXCs):

```sh
pip install .                 # or pipx / venv as you prefer
sudo mkdir -p /opt/frigate-sidecar/data /etc/frigate-sidecar
sudo cp config/sidecar.example.yml /etc/frigate-sidecar/sidecar.yml
sudo $EDITOR /etc/frigate-sidecar/sidecar.yml
# adjust sidecar.db_path to /opt/frigate-sidecar/data/frigate-sidecar.db
sudo cp contrib/frigate-sidecar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now frigate-sidecar.service
```

## Auth

**Every endpoint the sidecar owns requires the client's Frigate session
cookie** — the triage UI, `/faces`, `/analysis`, `/toybox` and `/v1` alike. The
sidecar has no user database and never holds a password: it validates the
cookie against `frigate.proxy_base_url` and caches the pass for
`sidecar.auth_cache_ttl_s`. Since the sidecar serves event history, face crops
of identified people, and writes that reach back into Frigate (labels, Frigate+
submissions, face-library promotion), an open sidecar is a way around Frigate's
own auth.

Three deliberate exceptions:

- `/v1/capabilities`, `/healthz`, `/version` — reachability probes a client
  needs *before* it has a session (plus `/static`).
- the reverse-proxy catch-all — Frigate authenticates that traffic itself, and
  its 401 + `WWW-Authenticate` challenge must reach the client intact.

Set `sidecar.require_frigate_auth: false` only if your Frigate has auth
disabled, in which case there is no session to check.

## API

All new endpoints are under `/v1`. Every `/v1` endpoint is optional: a client
should degrade to talking to bare Frigate directly when `/v1/capabilities`
reports nothing.

Full contract, error vocabulary, and rationale: [`docs/scrub-cache-and-proxy-spec.md`](docs/scrub-cache-and-proxy-spec.md).

| Method | Path | What it does |
|---|---|---|
| `GET` | `/v1/capabilities` | Unauthenticated. Reports whether the scrub cache and proxy are enabled, which cameras have generated data, and the sidecar version. |
| `GET` | `/v1/coverage/{camera}?start=&end=` | Recording coverage read live from `frigate.db` — what Frigate actually recorded in `[start, end)`, plus `latest_segment_end` (diagnostic) and `authoritative_through` (the boundary the client should actually trust). `recording_retention_days` comes from Frigate's own `record` config (the outer bound of the continuous and motion bands); `scrub_retention_days` is the cache's separate, usually shorter horizon — **do not read one as the other**. ETag'd. |
| `GET` | `/v1/scrub/{camera}/coverage?start=&end=` | Scrub-cache coverage — which buckets of sprite data exist, distinct from recording coverage. Compares against `retention_days` so the client can tell "will never be generated" from "still lagging". |
| `GET` | `/v1/scrub/{camera}/sheets?start=&end=` | Index of sprite sheets covering the window: immutable, content-addressed URLs (`{start}-{interval}-{count}`) plus grid geometry. |
| `GET` | `/v1/scrub/{camera}/sheet/{start}-{interval}-{count}.{jpg,webp}` | One sprite-sheet image. Every distinct fill-count is its own immutable object — no cache-freshness reasoning needed anywhere in the path. |
| `GET` | `/v1/motion/{camera}?start=&end=&scale=` | Totalized motion at any `scale`, always covering the full requested range, zero-filled where there's genuinely no data (works around two measured gaps in Frigate's own `/api/.../activity/motion`). |
| `GET` | `/v1/reel/{camera}?start=&end=&motion_scale=` | One call per reel window: coverage + scrub buckets (as `frames[]`) + motion + events, one cache lifetime. ETag'd. |
| `GET` | `/v1/highlights/{camera}?before=&limit=&order=&cluster_s=` | Recent tracked-object events (`reason` is a Frigate label: person/car/package/…) for jump-to-highlight UI. **Raw events, newest first, by default** — see below. |
| `*` | `/{path:path}` (catch-all) | Transparent reverse proxy to Frigate's authenticated origin (`frigate.proxy_base_url`) — `/api/*`, `/vod/*`, `/live/*`, `/preview/*`, everything else. Forwards `Range`/`Authorization`/`Cookie`/`Accept-Encoding`, streams the body **raw** so `content-encoding` and `content-length` stay consistent, relays `content-range`/`etag`/`location`/`www-authenticate` (incl. 401) unchanged, emits each `Set-Cookie` separately, and passes the HTTP method through (not GET-only). Registered last, so `/v1/*`, `/static`, and `/healthz` always win first. |
| `WS` | `/{path:path}` (catch-all) | WebSocket relay to the same origin — Frigate's `/ws` state feed and go2rtc's WebRTC signalling, so live view works through the single base URL. |

Unknown paths under endpoints the sidecar owns (not the proxy catch-all) return
JSON 404s, never HTML: `{"error": "<code>", "message": "..."}`.

### Highlights are events, not destinations

`/v1/highlights` returns **raw tracked-object events**, newest first. That is
worth stating plainly because the endpoint's purpose ("take me to the next
interesting thing") implies destinations, and events cluster hard: measured
across three cameras, 40–50% of consecutive highlights are less than 45s apart
(39/99, 46/99, 49/99, with median gaps of 306s / 57s / 48s). One person walking
past emits three or four, so an unclustered "next highlight" control presses the
same person four times.

Two opt-in parameters, both off by default so existing consumers see no change:

- `cluster_s=45` groups events within that many seconds into one destination at
  the run's earliest start, with `events` counting the members, `end` the latest
  end, and `reason`/`score` taken from the most confident member. Gaps are
  measured end-to-start, so a long event followed closely by another counts as
  continuing.
- `order=score` ranks by peak confidence rather than recency. Recency stays the
  default because a client scanning for adjacency depends on time order.

`limit` bounds the **events considered**, not the destinations returned, and its
reach in wall-clock time varies enormously with how busy a camera is: `limit=100`
covered 3.4 hours on one camera and 152 hours on another on the reference
deployment. A client filtering for a sparse label (`package` was 3 in 100) must
page; one call is never enough on its own.

`score` is the event's peak confidence. Current Frigate keeps it in the `data`
JSON blob and leaves the `score`/`top_score` columns NULL, so anything reading
the column alone reports null for every event.

## CLI

The same code is available as a CLI inside the container:

```sh
docker exec frigate-sidecar fsc --help
docker exec frigate-sidecar fsc triage sample --days 7 --n 30
docker exec frigate-sidecar fsc analysis score-histogram --days 7
```

Scrub-cache generation is also driven from the CLI (`fsc scrub ...`), run
continuously in-process by the server (never hourly — see §5.4 of the spec),
which also sweeps retention every `scrub.prune_interval_s`. The CLI stays
useful for backfill/maintenance:

```sh
docker exec frigate-sidecar fsc scrub generate            # one generation cycle, all configured cameras
docker exec frigate-sidecar fsc scrub generate --camera doorbell
docker exec frigate-sidecar fsc scrub backfill --camera doorbell --days 4
docker exec frigate-sidecar fsc scrub prune                # drop sheets/buckets past scrub.retention_days
docker exec frigate-sidecar fsc scrub coverage --camera doorbell
```

## Configuration

All values are settable in `config/sidecar.yml` or via environment variables.
Env vars use the prefix `FRIGATE_SIDECAR_` and nest with `__`:

```sh
FRIGATE_SIDECAR_FRIGATE__BASE_URL=http://frigate.lan:5000
FRIGATE_SIDECAR_SIDECAR__BIND_PORT=5001
```

Two settings sections back the new features:

- `scrub.*` — off by default (`scrub.enabled: false`). When enabled, generates
  sprite sheets for `scrub.cameras` (empty = all cameras) at
  `scrub.recent_interval_s` (default 1 fps) thinning to `scrub.aged_interval_s`
  after `scrub.aged_after_h`, capped at `scrub.retention_days` (default 4 —
  matches this deployment's measured continuous-recording window, *not*
  Frigate's `record.retain.days`). `scrub.cache_dir` **must** be on a
  different filesystem from `frigate.recordings_path`; the sidecar refuses to
  enable the cache at startup otherwise. `scrub.format` is `jpeg` by default
  (measured smaller than `webp` on real camera content).
- `frigate.proxy_base_url` — Frigate's *authenticated* origin (typically
  `:8971`), used only by the reverse proxy for app traffic. Kept separate from
  `frigate.base_url` (unauthenticated, used for the sidecar's own
  server-to-server calls) — do not point both at the same port.
- `scrub.preserve_source_aspect` (default on) — derive each camera's cell height
  from its own display aspect ratio rather than scaling every source into a
  fixed `cell_w × cell_h`. A 4:3 camera rendered into a 16:9 cell comes out
  anamorphically squeezed and nothing downstream can undo it; two of the ten
  cameras on the reference deployment are 1600×1200. The dimensions travel per
  sheet in the `cell_w`/`cell_h` metadata, so a client reading those renders each
  camera correctly. `cell_h` becomes the fallback for sources whose shape can't
  be measured.
- `scrub.match_keyframe_cadence` (default on) — generate a camera at its own
  keyframe cadence when that is coarser than `recent_interval_s`. A source whose
  GOP is longer than the target interval can only reach that interval by
  decoding every frame, at roughly 5× the cost of keyframe extraction, to
  synthesise stills the encoder never made distinct. On the reference deployment
  the three UniFi Protect cameras (5s GOP, against 1s on the seven Dahua ones)
  were ~70% of the generator's total work while being 30% of the fleet; matching
  their cadence cut cycle time from ~500s to ~150s. The interval travels per
  bucket in `interval`, so a camera generating at 5s is contract-compatible — it
  simply yields a still every 5s while scrubbing. Turn it off to force the
  configured interval everywhere and pay the decode.
- `frigate.recordings_path` — the host-side path that **replaces**
  `frigate.media_path` in `recordings.path`. Rows read
  `<media_path>/recordings/<date>/...`, so the `recordings/` segment comes from
  the DB value and must not be repeated here; pointing it at
  `…/recordings/recordings` maps every segment one level too deep and
  generation produces nothing. Deployment-specific; verify with `docker inspect`
  on the Frigate host per §8.2 of the spec, then confirm against a real
  `recordings.path` row. Startup logs an error if the path doesn't resolve, and
  the generator warns when segment files don't map.
- `sidecar.require_frigate_auth` — see [Auth](#auth) above. On by default.

## Status

Early. The triage UI and analysis tools were originally a set of ad-hoc scripts
shipped alongside one specific Frigate deployment; this repo promotes them to a
reusable service. Expect rough edges.

## License

MIT — see [LICENSE](LICENSE).
