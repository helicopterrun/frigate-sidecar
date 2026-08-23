---
title: First run
section: getting-started
order: 2
config: ["frigate", "sidecar", "log_level"]
---

The sidecar reads one YAML file, `config/sidecar.yml` (copy it from
`config/sidecar.example.yml`). Every value can also be set with environment
variables using the `FRIGATE_SIDECAR_` prefix and `__` for nesting
(`FRIGATE_SIDECAR_FRIGATE__BASE_URL=…`).

## The `frigate:` section — how the sidecar reaches Frigate

- `base_url` — Frigate's **unauthenticated** port (usually `:5000`). Used for
  server-to-server calls only; browsers are never pointed here.
- `proxy_base_url` — Frigate's **authenticated** origin (usually `:8971`).
  The sidecar forwards your browser traffic here and validates your session
  against it. These two URLs must be different, or the login check becomes a
  no-op.
- `db_path` — Frigate's SQLite database, opened **read-only**. The sidecar
  never writes to Frigate's database.
- `media_path` / `recordings_path` — where Frigate's recordings live from the
  sidecar's point of view. If scrubbing shows no images, this mapping is the
  most common culprit.

## The `sidecar:` section — the sidecar itself

- `db_path` — the sidecar's own SQLite file (labels, scrub cache index,
  push devices, faces). Created automatically.
- `host` / `port` — where the web app listens.
- `require_frigate_auth` — when on, every sidecar page requires signing in
  with your Frigate account (see [Troubleshooting](/guide/troubleshooting)).

`log_level` (top level) defaults to `INFO`.

```walkthrough
- Copy config/sidecar.example.yml to config/sidecar.yml
- Set frigate.base_url and frigate.proxy_base_url to your Frigate ports
- Point frigate.db_path at Frigate's frigate.db
- Start the service and open the Status page
- Check /healthz shows every component "ok" or "disabled"
```

When something looks wrong, `/healthz` is the first stop — it reports each
subsystem (database, MQTT, scrub cache, face enrichment) with a one-word
status.
