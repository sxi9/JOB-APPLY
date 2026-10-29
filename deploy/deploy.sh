#!/usr/bin/env bash
# Deploy JOB-APPLY to an Ubuntu box. Run as root (or with sudo) from a checkout, or with a git URL.
#
#   sudo deploy/deploy.sh                     # deploy this checkout to /opt/jobapply
#   sudo deploy/deploy.sh --branch main       # pull that branch into /opt/jobapply first
#
# Idempotent: creates the jobapply user, installs uv and Chromium deps, syncs the venv, runs migrations,
# installs the systemd units and restarts the services. Never touches .env or data/.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/jobapply}
APP_USER=${APP_USER:-jobapply}
BRANCH=""
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "creating user $APP_USER"
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/backups"

if [[ -n "$BRANCH" ]]; then
  if [[ -d "$APP_DIR/.git" ]]; then
    log "pulling $BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout -q "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
  else
    echo "$APP_DIR is not a git checkout; clone it first or deploy from a checkout without --branch" >&2
    exit 1
  fi
elif [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  log "syncing $SRC_DIR -> $APP_DIR"
  rsync -a --delete --exclude .venv --exclude data --exclude backups --exclude .env --exclude '*.db*' \
    --exclude .git --exclude .pytest_cache --exclude .ruff_cache "$SRC_DIR/" "$APP_DIR/"
fi

log "system packages"
apt-get update -qq
apt-get install -y -qq rsync sqlite3 curl ca-certificates >/dev/null

if [[ ! -x "$APP_DIR/.local/bin/uv" ]]; then
  log "installing uv"
  sudo -u "$APP_USER" env HOME="$APP_DIR" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
fi
UV="$APP_DIR/.local/bin/uv"

log "python environment"
sudo -u "$APP_USER" env HOME="$APP_DIR" "$UV" sync --frozen --no-dev --extra jobspy --project "$APP_DIR"

log "chromium for playwright"
sudo -u "$APP_USER" env HOME="$APP_DIR" "$UV" run --project "$APP_DIR" playwright install chromium
"$APP_DIR/.venv/bin/playwright" install-deps chromium >/dev/null 2>&1 || \
  sudo -u "$APP_USER" env HOME="$APP_DIR" "$UV" run --project "$APP_DIR" playwright install-deps chromium

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "creating .env from .env.example (fill it in!)"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sed -i "s|^JOBAPPLY_SECRET_KEY=.*|JOBAPPLY_SECRET_KEY=$(openssl rand -hex 32)|" "$APP_DIR/.env"
  sed -i "s|^JOBAPPLY_VAULT_KEY=.*|JOBAPPLY_VAULT_KEY=$(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/jobapply" vault-key)|" "$APP_DIR/.env"
  sed -i "s|^JOBAPPLY_DATABASE_URL=.*|JOBAPPLY_DATABASE_URL=sqlite:///$APP_DIR/data/jobapply.db|" "$APP_DIR/.env"
  sed -i "s|^JOBAPPLY_DATA_DIR=.*|JOBAPPLY_DATA_DIR=$APP_DIR/data|" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "database migrations"
sudo -u "$APP_USER" env HOME="$APP_DIR" bash -c "set -a; . '$APP_DIR/.env'; set +a; '$APP_DIR/.venv/bin/jobapply' db upgrade"

log "systemd units"
install -m 644 "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now jobapply-dashboard jobapply-collector jobapply-worker jobapply-backup.timer
systemctl restart jobapply-dashboard jobapply-collector jobapply-worker

log "done. Dashboard on http://$(hostname -I | awk '{print $1}'):8000 (put it behind Tailscale)."
log "Create the first admin: sudo -u $APP_USER $APP_DIR/.venv/bin/jobapply create-user admin --admin"
