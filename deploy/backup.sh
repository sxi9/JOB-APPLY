#!/usr/bin/env bash
# Nightly backup: consistent SQLite copy plus data/ (resumes, screenshots, browser profiles) and .env.
# Keeps the last $KEEP archives in $BACKUP_DIR. Restore: stop services, extract over /opt/jobapply, start.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/jobapply}
BACKUP_DIR=${BACKUP_DIR:-$APP_DIR/backups}
KEEP=${KEEP:-14}
DB_URL=${JOBAPPLY_DATABASE_URL:-sqlite:///$APP_DIR/data/jobapply.db}
DB_PATH=${DB_URL#sqlite:///}
STAMP=$(date +%Y%m%d-%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$BACKUP_DIR"
if [[ -f "$DB_PATH" ]]; then
  sqlite3 "$DB_PATH" ".backup '$WORK/jobapply.db'"
fi
[[ -f "$APP_DIR/.env" ]] && cp "$APP_DIR/.env" "$WORK/.env"
[[ -d "$APP_DIR/data" ]] && rsync -a --exclude '*.db*' --exclude home/.cache "$APP_DIR/data/" "$WORK/data/"
[[ -d "$APP_DIR/vault" ]] && rsync -a "$APP_DIR/vault/" "$WORK/vault/"

ARCHIVE="$BACKUP_DIR/jobapply-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" .
chmod 600 "$ARCHIVE"
ls -1t "$BACKUP_DIR"/jobapply-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup written: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
