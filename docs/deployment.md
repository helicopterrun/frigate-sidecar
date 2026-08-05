# Deployment notes

The README's [Install](../README.md#install) section covers the common paths.
This file collects the details that don't fit there.

## Layout

| Path | Purpose |
|---|---|
| `/opt/frigate-sidecar` | install dir (`.env`, compose file or venv, `data/`) |
| `/opt/frigate-sidecar/data` | sidecar SQLite DB + scrub sprite cache (read-write) |
| `config/sidecar.yml` (Docker) or `/etc/frigate-sidecar/sidecar.yml` (systemd) | configuration; generate with `fsc init`, full reference in `config/sidecar.example.yml` |

Frigate's config, database **directory** (WAL — never mount `frigate.db`
alone), and recordings tree are consumed read-only.

## systemd

`contrib/frigate-sidecar.service` is the reference unit; `install.sh` writes a
copy with the venv path substituted. Notes:

- `KillMode=mixed` is deliberate: the default control-group kill SIGTERMs
  in-flight ffmpeg children out from under the scrub generator, which then
  reads as a camera fault. Keep it.
- The unit runs as root for simplicity; a dedicated user works if it can read
  Frigate's DB/recordings and write the data dir.

## Optional units

- `contrib/frigate-watchdog.service` — external Frigate health watchdog
  (`watchdog.enabled: true`); restarts the Frigate container when its backend
  hangs while the container still reads "Up". Needs access to the Docker CLI.
- `contrib/frigate-sidecar-faces.service` + `.timer` — periodic face-crop
  scoring/curation job (`face.enabled: true`).

Both are opt-in and independent of the main server.

## Networking

`network_mode: host` is the default because the sidecar needs to reach
Frigate's two origins (unauthenticated `:5000` and authenticated `:8971`) and,
for push, the MQTT broker — usually all LAN addresses. Bridged networking
works too: publish `5001` and make sure `frigate.base_url` /
`frigate.proxy_base_url` / `push.mqtt_host` resolve from inside the container.

## Releases

Tagging `vX.Y.Z` (matching `__version__` in `src/frigate_sidecar/__init__.py`)
runs `.github/workflows/release.yml`: multi-arch image to
`ghcr.io/helicopterrun/frigate-sidecar` (`latest`, `X.Y`, `vX.Y.Z`) plus an
sdist/wheel attached to the GitHub Release.
