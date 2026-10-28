# Cloud build split

Which parts are built in Claude Code on the web (no logins, no local machine, no real credentials) versus locally on the laptop with access to the Ubuntu box, browser sessions and the credential vault.

## Build in the cloud (push to this repo)

Everything here runs against public endpoints or can be tested with fakes.

| Module | Why it is cloud-safe |
|---|---|
| Repo scaffold: `pyproject.toml` (uv, Python 3.11), package layout, config loading, logging | pure code |
| Database: SQLModel models for users, jobs, job_sources, applications, answers_bank, form_profiles, credentials (encrypted blob column only), migrations | pure code |
| Collectors for public ATS APIs: Greenhouse, Lever, Ashby, Workable, SmartRecruiters | no auth, public JSON |
| Collectors for job APIs: Remotive, RemoteOK, Jobicy, Arbeitnow, The Muse, HN Who is hiring, YC | no auth or free key read from env; tests use recorded fixtures |
| Collectors via python-jobspy: Indeed, LinkedIn guest search, Glassdoor, ZipRecruiter | no login for listing; tests use fixtures |
| Adzuna and JSearch adapters | key from env, tests use fixtures |
| ATS detector for company careers URLs | public HTTP |
| Dedupe and filter engine | pure code |
| Scheduler (APScheduler) wiring | pure code |
| Dashboard: FastAPI, auth with username/password and roles, all pages in DASHBOARD.md, HTMX templates, dark theme, mobile layout | runs on localhost in the cloud sandbox |
| AI layer: scoring, resume variant, cover letter, question answering; Claude API client with a fake backend for tests | key from env, fake in tests |
| Form profile engine: YAML profile loader, fingerprinting, learned profile store, answer bank | pure code |
| Playwright fillers for Greenhouse, Lever, Ashby, Workable, SmartRecruiters in dry-run mode | public application forms, no login; dry run never submits |
| Generic AI form mapper (accessibility tree to vault keys) | tested against saved HTML fixtures |
| IMAP OTP and link reader, SMTP sender | tested with a fake IMAP server (aioimaplib or a local Dovecot in Docker) |
| Inbox classifier and follow-up scheduler | fake mailbox in tests |
| Notifier interface with WhatsApp HTTP backend (`POST /send` to the existing bridge) | stubbed in tests |
| Deployment files: systemd unit files, Dockerfile, `deploy.sh`, backup script | files only, not run |
| Tests for all of the above, CI workflow on GitHub Actions | runs in the cloud |

## Build and run locally (laptop + Ubuntu box)

| Module | Why local |
|---|---|
| Credential vault with real values, `.env` on the box | real secrets |
| LinkedIn Easy Apply, Naukri, Instahyre, Cutshort, Wellfound assisted mode | real logged-in browser profiles, ban risk, needs eyes on it |
| Workday and other per-tenant signup flows with real OTP | real mailbox |
| Real OTP and verification-link end-to-end test | real mailbox |
| Switching any site from dry-run to auto-submit | real applications |
| Deploy to the Ubuntu box, Tailscale, backups | LAN access |
| Live tuning from dry-run screenshots | human review |

## Brief for the cloud session

Paste this into Claude Code on the web pointed at `sxi9/JOB-APPLY`:

```
Read README.md and every file in docs/. Build the "Build in the cloud" list from docs/CLOUD_BUILD.md, in this order, committing after each step on main:

1. Scaffold: uv project, Python 3.11, package `jobapply`, settings via pydantic-settings reading .env, structured logging, `jobapply` CLI with `collect`, `score`, `apply --dry-run`, `serve` commands.
2. Database: SQLModel models from docs/PLAN.md and docs/SOURCES.md (users, jobs, job_sources, applications, answers_bank, form_profiles, credentials, collector_runs). SQLite by default. Alembic migrations.
3. Collectors: one adapter per source with a shared `BaseCollector` returning the normalized job record in docs/SOURCES.md. Start with Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Remotive, RemoteOK, Jobicy, Arbeitnow, HN Who is hiring, then python-jobspy for Indeed/LinkedIn guest/Glassdoor/ZipRecruiter, then Adzuna and JSearch. Company list in `config/companies.yaml` with the ATS detector filling in the ATS type. Record fixtures for tests.
4. Dedupe and per-user filter engine, scheduler with APScheduler.
5. Dashboard: FastAPI + Jinja2 + HTMX, every page in docs/DASHBOARD.md, login with roles, admin sees all users, dark theme, mobile friendly. Seed script with a demo admin and demo user, demo jobs so the UI can be reviewed without any API key.
6. AI layer behind an interface with a `FakeLLM` for tests and a Claude backend: job scoring, resume variant from a JSON Resume master rendered to PDF, cover letter, answer generation for the answers bank.
7. Form engine: YAML profiles per docs/FORM_PROFILES.md for Greenhouse, Lever, Ashby, Workable, SmartRecruiters; Playwright runner with dry-run that fills, screenshots and never submits; fingerprinting and learned profile store; generic AI mapper from accessibility tree.
8. Mail: IMAP reader that finds OTP codes and verification links by sender and regex, SMTP sender, inbox classifier, follow-up scheduler. Tests with a fake IMAP server.
9. Notifier interface with a WhatsApp HTTP backend (configurable base URL and token).
10. Deployment files: systemd units for collector, scheduler and dashboard, deploy.sh, backup.sh, Dockerfile. GitHub Actions CI running tests.

Rules: never commit secrets, never submit a real application, keep every network call behind an adapter with recorded fixtures for tests, keep multi-user isolation (every query scoped by user_id). Do not add Co-Authored-By or Generated-with lines to commits. Update docs/STATUS.md after each step with what works and what is stubbed.
```

## Continue locally after the cloud build

1. Pull main on the laptop and the Ubuntu box.
2. Fill `.env` and the vault with real credentials and the mailbox.
3. Run collectors for real, review jobs in the dashboard.
4. Dry-run the ATS fillers on real postings, review screenshots, flip to auto-submit site by site.
5. Build the assisted sites (LinkedIn, Naukri, Instahyre) with real browser profiles.
6. Deploy with the systemd units, put the dashboard behind Tailscale.
