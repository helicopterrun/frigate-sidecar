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

## API

All new endpoints are under `/v1` and require the same Frigate session cookie
the client already sends to `/api/*` — `/v1` is never less protected than the
endpoints it sits beside. `/v1/capabilities` is the one exception (unauthenticated,
so a client can probe reachability before it has a session). Every `/v1`
endpoint is optional: a client should degrade to talking to bare Frigate
directly when `/v1/capabilities` reports nothing.

Full contract, error vocabulary, and rationale: [`docs/scrub-cache-and-proxy-spec.md`](docs/scrub-cache-and-proxy-spec.md).

| Method | Path | What it does |
|---|---|---|
| `GET` | `/v1/capabilities` | Unauthenticated. Reports whether the scrub cache and proxy are enabled, which cameras have generated data, and the sidecar version. |
| `GET` | `/v1/coverage/{camera}?start=&end=` | Recording coverage read live from `frigate.db` — what Frigate actually recorded in `[start, end)`, plus `latest_segment_end` (diagnostic) and `authoritative_through` (the boundary the client should actually trust). ETag'd. |
| `GET` | `/v1/scrub/{camera}/coverage?start=&end=` | Scrub-cache coverage — which buckets of sprite data exist, distinct from recording coverage. Compares against `retention_days` so the client can tell "will never be generated" from "still lagging". |
| `GET` | `/v1/scrub/{camera}/sheets?start=&end=` | Index of sprite sheets covering the window: immutable, content-addressed URLs (`{start}-{interval}-{count}`) plus grid geometry. |
| `GET` | `/v1/scrub/{camera}/sheet/{start}-{interval}-{count}.{jpg,webp}` | One sprite-sheet image. Every distinct fill-count is its own immutable object — no cache-freshness reasoning needed anywhere in the path. |
| `GET` | `/v1/motion/{camera}?start=&end=&scale=` | Totalized motion at any `scale`, always covering the full requested range, zero-filled where there's genuinely no data (works around two measured gaps in Frigate's own `/api/.../activity/motion`). |
| `GET` | `/v1/reel/{camera}?start=&end=&motion_scale=` | One call per reel window: coverage + scrub buckets (as `frames[]`) + motion + events, one cache lifetime. ETag'd. |
| `GET` | `/v1/highlights/{camera}?before=&limit=` | Ranked recent tracked-object events (`reason` is a Frigate label: person/car/package/…) for jump-to-highlight UI. |
| `*` | `/{path:path}` (catch-all) | Transparent reverse proxy to Frigate's authenticated origin (`frigate.proxy_base_url`) — `/api/*`, `/vod/*`, `/live/*`, `/preview/*`, everything else. Forwards `Range`/`Authorization`/`Cookie`, relays `content-range`/`etag`/`set-cookie`/`www-authenticate` (incl. 401) unchanged, and passes the HTTP method through (not GET-only). Registered last, so `/v1/*`, `/static`, and `/healthz` always win first. |

Unknown paths under endpoints the sidecar owns (not the proxy catch-all) return
JSON 404s, never HTML: `{"error": "<code>", "message": "..."}`.

## CLI

The same code is available as a CLI inside the container:

```sh
docker exec frigate-sidecar fsc --help
docker exec frigate-sidecar fsc triage sample --days 7 --n 30
docker exec frigate-sidecar fsc analysis score-histogram --days 7
```

Scrub-cache generation is also driven from the CLI (`fsc scrub ...`), run
continuously in-process by the server (never hourly — see §5.4 of the spec)
but scriptable for backfill/maintenance:

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
- `frigate.recordings_path` — host-side path to Frigate's recordings root, as
  the sidecar itself sees it. Deployment-specific; verify with `docker inspect`
  on the Frigate host per §8.2 of the spec before trusting the default.

## Status

Early. The triage UI and analysis tools were originally a set of ad-hoc scripts
shipped alongside one specific Frigate deployment; this repo promotes them to a
reusable service. Expect rough edges.

## License

MIT — see [LICENSE](LICENSE).
