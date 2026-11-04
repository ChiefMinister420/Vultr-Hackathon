#!/usr/bin/env bash
# One-time (and re-runnable) host setup for Slipstream on Ubuntu 24.04.
# Run as root from the repository clone at /opt/slipstream:  sudo bash deploy/setup.sh
# It never reads, prints or writes /etc/slipstream.env; create that file yourself (see docs).
set -euo pipefail

APP_DIR=/opt/slipstream
APP_USER=slipstream
VENV="$APP_DIR/.venv"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/setup.sh" >&2
  exit 1
fi
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
  echo "Clone the repository to $APP_DIR (deploy/slipstream.service expects it there)." >&2
  exit 1
fi

log "Installing Docker, Python venv support and tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io python3-venv ca-certificates curl gnupg
systemctl enable --now docker

log "Installing gVisor (runsc) from gVisor's apt repository"
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://gvisor.dev/archive.key \
  | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update -q
apt-get install -y -q runsc

log "Registering runsc with Docker"
runsc install
systemctl restart docker

log "Creating the $APP_USER service user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/slipstream --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
# Docker group membership is root-equivalent on this host; this user runs nothing else.
usermod -aG docker "$APP_USER"

log "Creating the virtualenv and installing the project"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$APP_DIR"

log "Building the runner and proxy images"
cd "$APP_DIR"
docker build -t slipstream-runner:local -f sandbox/Dockerfile .
docker build -t slipstream-proxy:local -f proxy/Dockerfile .

log "Verifying gVisor"
# Captured first: with pipefail, grep -q closing the pipe early could fail the check.
dmesg_out="$(docker run --rm --runtime=runsc alpine dmesg)" || dmesg_out=""
if grep -q gVisor <<<"$dmesg_out"; then
  echo "runsc works: the container kernel is gVisor."
else
  echo "runsc check failed: 'docker run --runtime=runsc alpine dmesg' did not show gVisor." >&2
  exit 1
fi

log "Done"
cat <<EOF
Next steps (docs/DEPLOY_VULTR.md):
  1. Create /etc/slipstream.env from deploy/slipstream.env.example (chmod 600).
  2. sudo cp deploy/slipstream.service /etc/systemd/system/ && sudo systemctl daemon-reload
     sudo systemctl enable --now slipstream
  3. Set up Caddy with deploy/Caddyfile.
EOF
