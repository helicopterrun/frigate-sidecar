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
- The unit runs as a dedicated `frigate-sidecar` system user under
  `ProtectSystem=strict` (install.sh creates the user and chowns the install
  dir and `/etc/frigate-sidecar`). Writable paths are only the install dir,
  the config dir, and the scrub cache (`ReadWritePaths` — install.sh derives
  the cache dir from the live config; add it by hand if you move it later).

### Granting the service user access to Frigate's files

The sidecar reads Frigate's `config.yml`, database directory, and recordings
tree. After the first install (or upgrade from a root unit), verify:

```sh
sudo -u frigate-sidecar test -r /opt/frigate/config.yml && echo config ok
sudo -u frigate-sidecar test -r /opt/frigate/database/frigate.db && echo db ok
sudo -u frigate-sidecar ls /mnt/frigate-storage/recordings >/dev/null && echo recordings ok
```

If any fail, either add the user to the group that owns those paths
(`usermod -aG <group> frigate-sidecar`, then restart the unit) or grant
ACLs directly:

```sh
setfacl -R -m u:frigate-sidecar:rX /mnt/frigate-storage/recordings
setfacl -m d:u:frigate-sidecar:rX /mnt/frigate-storage/recordings   # future files
setfacl -R -m u:frigate-sidecar:rX /opt/frigate/database
setfacl -m u:frigate-sidecar:r /opt/frigate/config.yml
```

Frigate runs SQLite in WAL mode, so the whole `database/` directory matters
(`frigate.db-wal`/`-shm` included). If the sidecar logs "unable to open
database file" despite read access, the `-shm` file needs group/ACL write
for read-only WAL clients on your SQLite build — extend the ACL to `rwX` on
`frigate.db-shm` only.

## Optional units

- `contrib/frigate-watchdog.service` — external Frigate health watchdog
  (`watchdog.enabled: true`); restarts the Frigate container when its backend
  hangs while the container still reads "Up". Needs access to the Docker CLI.

It is opt-in and independent of the main server.

## Networking

`network_mode: host` is the default because the sidecar needs to reach
Frigate's two origins (unauthenticated `:5000` and authenticated `:8971`) and,
for push, the MQTT broker — usually all LAN addresses. Bridged networking
works too: publish `5001` and make sure `frigate.base_url` /
`frigate.proxy_base_url` / `push.mqtt_host` resolve from inside the container.

## Backup / restore

`fsc backup <dest>` writes the sidecar's own state: the SQLite DB, the
`.session_secret` signing key (losing it just signs every remember-me device
out — regenerated on next start if missing), and the resolved `sidecar.yml`.
The scrub cache and face-model directories are NOT included — both are
regenerable from Frigate's own recordings/DB and are usually far larger.
`<dest>` is a plain directory, or a single file if named `*.tar.gz`.

Systemd stop/restore/start:

```sh
sudo systemctl stop frigate-sidecar
sudo -u frigate-sidecar fsc backup /opt/frigate-sidecar/backups/$(date +%F).tar.gz
# ... or, to restore:
sudo -u frigate-sidecar fsc restore /opt/frigate-sidecar/backups/2026-08-30.tar.gz --force
sudo systemctl start frigate-sidecar
curl -s localhost:5001/healthz
```

`fsc restore` refuses to run at all without `--force` (a restore under a
running service can corrupt the DB's WAL — `--force` is your confirmation
that the unit above is stopped). Restoring a backup taken before a schema
migration is fine: the first open after the restore re-creates any newer
tables and columns (empty) — a warning is logged so you know it happened.

A cron example for a nightly backup while the service stays up (safe: the DB
copy uses SQLite's own online backup API, not a raw file copy):

```cron
15 3 * * * frigate-sidecar fsc backup /opt/frigate-sidecar/backups/$(date +\%F).tar.gz
```

Prune old backups yourself (e.g. `find backups/ -mtime +30 -delete` in the
same crontab) — `fsc backup`/`restore` don't manage retention.

## Releases

Tagging `vX.Y.Z` (matching `__version__` in `src/frigate_sidecar/__init__.py`)
runs `.github/workflows/release.yml`: multi-arch image to
`ghcr.io/helicopterrun/frigate-sidecar` (`latest`, `X.Y`, `vX.Y.Z`) plus an
sdist/wheel attached to the GitHub Release.

### High-res cross-camera face capture (optional)

`frigate-sidecar-face-capture.timer` grabs the *capture* camera's full
main-stream frame out of Frigate's recordings whenever a `person` event fires on
a *trigger* camera, for human review at `/faces/captures`.

```sh
sudo install -m 0644 contrib/frigate-sidecar-face-capture.service \
                     contrib/frigate-sidecar-face-capture.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now frigate-sidecar-face-capture.timer
```

Configure under `face_capture:` in `sidecar.yml` — at minimum `enabled`,
`trigger_cameras`, `capture_camera`, and an `output_dir` **inside
/opt/frigate-sidecar** (the main unit runs `ProtectSystem=strict` with
`ReadWritePaths=/opt/frigate-sidecar`, so anything outside fails EROFS at write
time rather than at config load).

It is a **oneshot behind a timer, not an in-process loop, and deliberately not an
MQTT hook**: `/api/{camera}/recordings/{ts}/snapshot.jpg` 404s until the segment
covering that timestamp has been committed, and segments commit at their *end*
(measured publish lag 5.4-9.4s per camera). `capture_delay_s` (default 45s) is
what makes a 404 genuinely terminal rather than "asked too early".

Checks: `python3 -m frigate_sidecar face-capture stats` (counts + last-run
heartbeat), `face-capture scan` (one manual pass), `face-capture prune`.

### Face enrichment (optional)

`face_enrich:` runs **inside the main service** as a lifespan worker (no extra
unit): for each ended `person` event on an enrolled camera it samples full-res
recording frames, embeds the best faces (InsightFace buffalo_l, CPU-only), and
clusters identities at `/enrich/clusters`. Naming a cluster makes later matches
write the event's `sub_label` back to Frigate — disable Frigate's own face
recognition on those cameras first so the sidecar is the only writer.

Setup:

1. `pip install "frigate-sidecar[enrich]"` (adds insightface + onnxruntime).
2. Set `face_enrich.enabled: true` and `cameras:` in `sidecar.yml`. The model
   pack (~300 MB) downloads into `model_dir` on the first cycle — keep it
   under `/opt/frigate-sidecar` (ProtectSystem=strict) and expect the first
   cycle to be slow; pre-warm with
   `python3 -c "from frigate_sidecar.faces.enrich import _engine; _engine('/opt/frigate-sidecar/data/models')"`
   as the service user if you want the download done before restart.
3. Restart and watch `/healthz` — a `face_enrich` check appears (ok/starting/
   stale) alongside the scrub one.
