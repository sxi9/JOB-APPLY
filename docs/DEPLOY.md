# Deploy

Two routes. Both keep `.env` and `data/` out of git and never overwrite them once present.

## Sudo-less (user services) — `deploy/deploy-user.sh`

For a box where you are a normal user. Installs `~/jobapply` from `origin/main`, a uv-managed Python 3.11, the
venv (`--extra jobspy`), Playwright's Chromium (system libs must already exist), a generated `.env`, runs the
migrations and installs four **user** systemd units in `~/.config/systemd/user/`:

| Unit | Runs |
|---|---|
| `jobapply-dashboard` | `jobapply serve` on `JOBAPPLY_HOST:JOBAPPLY_PORT` from `.env` (default `127.0.0.1:8010`) |
| `jobapply-collector` | `jobapply schedule -g collector` (collect + hot tier) |
| `jobapply-worker` | `jobapply schedule -g worker` (score, apply, inbox, follow-ups, summary) |
| `jobapply-backup.timer` | nightly `deploy/backup.sh` → `~/jobapply/backups/` (keeps 14) |

```bash
curl -LsSf https://raw.githubusercontent.com/sxi9/JOB-APPLY/main/deploy/deploy-user.sh | bash
```

or from a checkout: `deploy/deploy-user.sh` (update) / `deploy/deploy-user.sh --no-pull` (reinstall only).
Needs `loginctl enable-linger $USER` once so the units survive logout. Manage with
`systemctl --user {status,restart,stop} jobapply-dashboard jobapply-collector jobapply-worker` and
`journalctl --user -u jobapply-dashboard -f`.

Reaching it: set `JOBAPPLY_HOST=0.0.0.0` in `.env` for LAN access and open the port in the host firewall
(root): `sudo ufw allow 8010/tcp` and, where firewalld is also active,
`sudo firewall-cmd --permanent --add-port=8010/tcp && sudo firewall-cmd --reload`. For HTTPS on a tailnet
instead: `tailscale serve --bg --https=8448 http://127.0.0.1:8010` (operator rights needed).

Migrating an existing local install: stop nothing locally, take a consistent copy with
`python -c "import sqlite3; sqlite3.connect('jobapply.db').backup(sqlite3.connect('snap.db'))"`, copy `snap.db`
to `~/jobapply/data/jobapply.db` and `data/resumes/` alongside, then `deploy/deploy-user.sh --no-pull`
(runs `db upgrade`). Browser profiles and cookie files are deliberately not copied: a logged-in session
moving to a new IP tends to trip the site's challenge; reconnect each site from Settings → Sites on the new host.

## Root (`/opt/jobapply`) — `deploy/deploy.sh`

System user `jobapply`, `/opt/jobapply`, hardened system units in `deploy/systemd/`. Same layout otherwise.
`sudo deploy/deploy.sh [--branch main]`.

## Current host (2026-10-29)

Ubuntu laptop on the LAN, user-services route, `~/jobapply`, dashboard `0.0.0.0:8010`, DB and resumes migrated
from the Windows laptop (users admin/demo/sai, 17.6k jobs). Firewall port 8010 not yet opened.
