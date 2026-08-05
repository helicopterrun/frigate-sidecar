#!/usr/bin/env bash
# frigate-sidecar installer.
#
#   curl -fsSL https://raw.githubusercontent.com/helicopterrun/frigate-sidecar/main/install.sh | bash
#
# Docker present  -> compose deployment in $INSTALL_DIR (image from ghcr.io)
# No Docker       -> bare-metal venv + systemd unit
#
# Idempotent: re-running upgrades (pulls the new image / pip upgrade) and
# restarts, leaving your .env / sidecar.yml untouched.
set -euo pipefail

REPO="helicopterrun/frigate-sidecar"
RAW="https://raw.githubusercontent.com/$REPO/main"
INSTALL_DIR="${INSTALL_DIR:-/opt/frigate-sidecar}"
IMAGE="ghcr.io/$REPO:latest"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo) -- installs into $INSTALL_DIR"

fetch() { # fetch <relpath> <dest> -- never clobber an existing user-edited file
  if [ -e "$2" ]; then
    say "keeping existing $2"
  else
    curl -fsSL "$RAW/$1" -o "$2"
  fi
}

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"

if command -v docker >/dev/null 2>&1; then
  say "Docker found -- installing compose deployment in $INSTALL_DIR"
  docker compose version >/dev/null 2>&1 || die "docker is present but 'docker compose' is not; install the compose plugin"

  cd "$INSTALL_DIR"
  fetch docker-compose.yml docker-compose.yml
  fetch .env.example .env.example
  fetch .env.example .env
  # data dir must be writable by the container's non-root uid
  chown -R 10001:10001 "$INSTALL_DIR/data"

  say "pulling $IMAGE"
  docker pull "$IMAGE"

  if [ ! -f "$INSTALL_DIR/config/sidecar.yml" ]; then
    if [ -t 0 ]; then
      say "generating config (answer the prompts; defaults suit a stock Frigate install)"
      docker compose run --rm frigate-sidecar init -o /config/sidecar.yml
    else
      warn "stdin is not a tty (curl|bash) -- writing a default config; edit $INSTALL_DIR/config/sidecar.yml"
      docker compose run --rm frigate-sidecar init --non-interactive -o /config/sidecar.yml
    fi
  fi

  say "starting"
  docker compose up -d
  say "done. Check: curl http://localhost:5001/healthz  |  logs: docker logs -f frigate-sidecar"
  say "Edit $INSTALL_DIR/.env (host paths) and $INSTALL_DIR/config/sidecar.yml, then: docker compose up -d"
else
  say "Docker not found -- installing bare-metal (venv + systemd) in $INSTALL_DIR"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  command -v systemctl >/dev/null 2>&1 || die "systemd is required for the bare-metal install"
  python3 - <<'EOF' || die "python >= 3.10 is required"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
  command -v ffmpeg >/dev/null 2>&1 || warn "ffmpeg not found -- required for the scrub cache; install it via your package manager"

  say "creating venv + installing package"
  python3 -m venv "$INSTALL_DIR/venv" 2>/dev/null || {
    die "python3 -m venv failed (on Debian/Ubuntu: apt install python3-venv)"
  }
  "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade frigate-sidecar 2>/dev/null || \
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "frigate-sidecar @ git+https://github.com/$REPO"

  mkdir -p /etc/frigate-sidecar
  if [ ! -f /etc/frigate-sidecar/sidecar.yml ]; then
    if [ -t 0 ]; then
      say "generating config (answer the prompts; defaults suit a stock Frigate install)"
      "$INSTALL_DIR/venv/bin/fsc" init -o /etc/frigate-sidecar/sidecar.yml \
        --sidecar-db "$INSTALL_DIR/data/frigate-sidecar.db"
    else
      warn "stdin is not a tty (curl|bash) -- writing a default config; edit /etc/frigate-sidecar/sidecar.yml"
      "$INSTALL_DIR/venv/bin/fsc" init --non-interactive -o /etc/frigate-sidecar/sidecar.yml \
        --sidecar-db "$INSTALL_DIR/data/frigate-sidecar.db"
    fi
  else
    say "keeping existing /etc/frigate-sidecar/sidecar.yml"
  fi

  say "installing systemd unit"
  cat > /etc/systemd/system/frigate-sidecar.service <<EOF
[Unit]
Description=Frigate Sidecar (triage UI + analysis)
Documentation=https://github.com/$REPO
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="FRIGATE_SIDECAR_CONFIG=/etc/frigate-sidecar/sidecar.yml"
ExecStart=$INSTALL_DIR/venv/bin/python -m frigate_sidecar serve
Restart=on-failure
RestartSec=5
# Signal only the main process on stop: the default (control-group) SIGTERMs
# in-flight ffmpeg children out from under the scrub generator.
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now frigate-sidecar.service
  systemctl restart frigate-sidecar.service
  say "done. Check: curl http://localhost:5001/healthz  |  logs: journalctl -fu frigate-sidecar"
  say "Config: /etc/frigate-sidecar/sidecar.yml (restart with: systemctl restart frigate-sidecar)"
fi
