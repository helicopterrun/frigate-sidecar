---
title: Deployment & upgrades
section: operations
order: 1
---

The sidecar runs as a single Python service beside Frigate — bare metal
(systemd), Docker Compose, or the `install.sh` quick path. Full details are
in the repo's `docs/deployment.md`; the essentials:

## Install extras

Optional features are Python "extras" — install only what you use:

| Extra | Enables |
|---|---|
| `http2` | faster proxying |
| `annotation` | annotation-offset analysis |
| `faces` | face crop curation ([Faces](/faces)) |
| `enrich` | identity clustering ([Identities](/enrich/clusters)) |

## Upgrading

1. Pull the new code.
2. Re-run `pip install ".[…extras…]"` — **don't skip this**: a code update
   without the install step runs new code against old dependencies and
   misses new ones entirely.
3. Restart the service.
4. Open [`/healthz`](/healthz) and confirm every component reports healthy.

Database migrations are automatic: the sidecar's schema is applied
idempotently at startup, and Frigate's database is only ever read.

## Where state lives

Everything the sidecar owns is in its SQLite file plus the scrub cache
directory — back those up and a reinstall recovers completely.
