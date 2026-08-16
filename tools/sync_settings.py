"""Sync the local push settings document to another sidecar instance.

The settings doc is pure policy (routing tables, zone classes/overrides,
camera calibration, secure area, LA prefs) — no device registrations or
tokens live in it — so a wholesale copy is the correct sync. The remote
sidecar loads settings at startup, hence the restart.

Usage:
    python tools/sync_settings.py --host nvr \
        [--local config/push_settings.json] \
        [--remote-path /opt/frigate-sidecar/config/push_settings.json] \
        [--service frigate-sidecar] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from frigate_sidecar.push import policy_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="ssh host/alias of the target instance")
    ap.add_argument("--local", default="config/push_settings.json")
    ap.add_argument("--remote-path", default="/opt/frigate-sidecar/config/push_settings.json")
    ap.add_argument("--service", default="frigate-sidecar")
    ap.add_argument("--dry-run", action="store_true", help="validate and diff, send nothing")
    args = ap.parse_args()

    local_path = Path(args.local)
    try:
        raw = json.loads(local_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {local_path}: {exc}", file=sys.stderr)
        return 1
    errors = policy_settings.validate_settings(raw)
    if errors:
        print("local settings are invalid; refusing to sync:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    doc = policy_settings.normalize_settings(raw)
    payload = json.dumps(doc, indent=2, sort_keys=True)

    # Show what changes on the remote before touching it.
    remote_raw = subprocess.run(
        ["ssh", args.host, f"cat {shlex.quote(args.remote_path)} 2>/dev/null || true"],
        capture_output=True, text=True,
    ).stdout
    try:
        remote_doc = policy_settings.normalize_settings(json.loads(remote_raw))
    except (json.JSONDecodeError, TypeError):
        remote_doc = {}
    changed = [
        key for key in sorted(set(doc) | set(remote_doc))
        if doc.get(key) != remote_doc.get(key)
    ]
    if not changed:
        print("remote already matches — nothing to sync.")
        return 0
    print("keys that differ:", ", ".join(changed))
    if args.dry_run:
        print("dry run — nothing sent.")
        return 0

    # Write via a temp file + rename so the remote never sees a torn file,
    # then restart so the running sidecar loads it.
    remote_tmp = args.remote_path + ".sync-tmp"
    write_cmd = (
        f"cat > {shlex.quote(remote_tmp)} && "
        f"mv {shlex.quote(remote_tmp)} {shlex.quote(args.remote_path)} && "
        f"systemctl restart {shlex.quote(args.service)} && sleep 2 && "
        f"systemctl is-active {shlex.quote(args.service)}"
    )
    result = subprocess.run(
        ["ssh", args.host, write_cmd], input=payload, capture_output=True, text=True,
    )
    out = (result.stdout or "").strip()
    if result.returncode != 0 or out.splitlines()[-1:] != ["active"]:
        print(f"sync failed: rc={result.returncode} out={out!r} err={result.stderr!r}",
              file=sys.stderr)
        return 1
    print(f"synced {len(changed)} changed key(s) to {args.host}; service active.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
