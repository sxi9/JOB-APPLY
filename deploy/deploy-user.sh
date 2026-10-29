#!/usr/bin/env bash
# Deploy JOB-APPLY as a normal user (no sudo): ~/jobapply checkout, uv-managed Python 3.11, user systemd
# units, nightly backup timer. Pair it with `tailscale serve` for HTTPS on the tailnet (needs the user to be
# the tailscale operator: `sudo tailscale set --operator=$USER` once).
#
#   deploy/deploy-user.sh                 # first install or update from origin/main
#   deploy/deploy-user.sh --no-pull       # reinstall units / resync venv without touching the checkout
#
# Idempotent. Never touches .env or data/ once they exist. Requires: git, curl, rsync, sqlite3 (for backups).
set -euo pipefail

APP_DIR=${APP_DIR:-$HOME/jobapply}
REPO=${REPO:-https://github.com/sxi9/JOB-APPLY.git}
BRANCH=${BRANCH:-main}
PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  log "cloning $REPO -> $APP_DIR"
  git clone -q "$REPO" "$APP_DIR"
elif [[ $PULL -eq 1 ]]; then
  log "updating $BRANCH"
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
fi
cd "$APP_DIR"
mkdir -p data backups

log "python environment"
uv python install 3.11 >/dev/null 2>&1 || true
uv sync --frozen --no-dev --extra jobspy

log "chromium for playwright"
uv run playwright install chromium >/dev/null

if [[ ! -f .env ]]; then
  log "creating .env from .env.example"
  cp .env.example .env
  chmod 600 .env
  sed -i "s|^JOBAPPLY_SECRET_KEY=.*|JOBAPPLY_SECRET_KEY=$(openssl rand -hex 32)|" .env
  sed -i "s|^JOBAPPLY_VAULT_KEY=.*|JOBAPPLY_VAULT_KEY=$(.venv/bin/jobapply vault-key)|" .env
  sed -i "s|^JOBAPPLY_DATABASE_URL=.*|JOBAPPLY_DATABASE_URL=sqlite:///$APP_DIR/data/jobapply.db|" .env
  sed -i "s|^JOBAPPLY_DATA_DIR=.*|JOBAPPLY_DATA_DIR=$APP_DIR/data|" .env
  printf 'JOBAPPLY_HOST=127.0.0.1\nJOBAPPLY_PORT=%s\n' "${PORT:-8010}" >> .env
fi

log "database migrations"
bash -c "set -a; . ./.env; set +a; .venv/bin/jobapply db upgrade"

log "user systemd units"
mkdir -p "$HOME/.config/systemd/user"
# Units assume APP_DIR=%h/jobapply; rewrite when deploying elsewhere.
for u in deploy/systemd-user/*; do
  sed "s|%h/jobapply|$APP_DIR|g" "$u" > "$HOME/.config/systemd/user/$(basename "$u")"
done
chmod +x deploy/backup.sh
systemctl --user daemon-reload
systemctl --user enable --now jobapply-dashboard jobapply-collector jobapply-worker jobapply-backup.timer
systemctl --user restart jobapply-dashboard jobapply-collector jobapply-worker

PORT_NOW=$(grep -E '^JOBAPPLY_PORT=' .env | cut -d= -f2)
log "done. Dashboard on http://127.0.0.1:${PORT_NOW:-8000}."
log "Tailnet HTTPS: tailscale serve --bg --https=8448 http://127.0.0.1:${PORT_NOW:-8000}"
log "First admin: .venv/bin/jobapply create-user <name> --admin   (prompts for the password)"
