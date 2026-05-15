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

Runs as a single Docker container next to Frigate, bind-mounting Frigate's
`config.yml` and `frigate.db` read-only and writing its own SQLite DB.

## Setup

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

## CLI

The same code is available as a CLI inside the container:

```sh
docker exec frigate-sidecar fsc --help
docker exec frigate-sidecar fsc triage sample --days 7 --n 30
docker exec frigate-sidecar fsc analysis score-histogram --days 7
```

## Configuration

All values are settable in `config/sidecar.yml` or via environment variables.
Env vars use the prefix `FRIGATE_SIDECAR_` and nest with `__`:

```sh
FRIGATE_SIDECAR_FRIGATE__BASE_URL=http://frigate.lan:5000
FRIGATE_SIDECAR_SIDECAR__BIND_PORT=5001
```

## Status

Early. The triage UI and analysis tools were originally a set of ad-hoc scripts
shipped alongside one specific Frigate deployment; this repo promotes them to a
reusable service. Expect rough edges.

## License

MIT — see [LICENSE](LICENSE).
