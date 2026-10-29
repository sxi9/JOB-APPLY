# Handoff — where things stand (2026-10-29)

The one-page context for whoever (or whatever) picks this up next. Per-step detail is in [STATUS.md](STATUS.md);
this is the map.

## What the product is

Multi-user job scraper + auto-apply. Anyone signs up (`/signup`), runs the onboarding wizard (profile, roles,
resume, answers, mailbox, site connections) and the system collects jobs across every sector, filters them to
that user's roles, scores and tailors a resume per job, and applies — fully automatic on public ATS boards,
assisted (logged-in, capped, gated) on LinkedIn / Naukri / Instahyre / Cutshort / Wellfound, account-per-company
on Workday, and via a universal navigator on any other host. AI is used both ways (scoring, letters, answers,
form recovery, messages, inbound mail triage) but every AI call has a non-AI fallback and the whole thing runs
with `JOBAPPLY_LLM_BACKEND=fake` (zero API tokens) — which is how it runs today.

## Where it runs

| Where | State |
|---|---|
| Windows laptop, `D:\physical_ai\JOB-APPLY` | dev checkout; dashboard started by hand (`uv run jobapply serve`), port 8000 |
| Ubuntu box `192.168.29.22`, `~/jobapply` | **production**: user systemd units `jobapply-dashboard` (0.0.0.0:8010), `jobapply-collector`, `jobapply-worker`, nightly `jobapply-backup.timer`. DB + resumes migrated from the laptop. Redeploy: `ssh box 'cd ~/jobapply && deploy/deploy-user.sh'`. Port 8010 is still blocked by ufw/firewalld (needs root). See [DEPLOY.md](DEPLOY.md). |

## Safety model (do not loosen without the owner)

Global dry-run (on), kill switch, per-site `auto_submit` (off everywhere), per-user and per-site daily caps,
active hours, challenge cooldowns. A CAPTCHA / OTP-not-received / login wall pauses and is never retried in a
loop. Final submits, account creation, outbound mail and referral sends happen only with dry-run off AND the
site's auto-submit on (referrals: plus the global referral switch and per-draft approval). `data/` is
git-ignored and holds everything personal (DB, cookies, browser profiles, resumes, screenshots); nothing in it
may ever be committed. Login to sites is cookie-export upload only; the app never types a password.

## What is live-verified vs fixture-only

- Live: all public ATS/feeds, LinkedIn guest search (429 → 2 h cooldown is normal), LinkedIn cookie login +
  Easy Apply dry runs, ATS form filling on Stripe/Palantir (CAPTCHA pauses as designed), Instahyre and Cutshort
  public search, Wellfound (Cloudflare-challenged from the home IP), Naukri API (406 recaptcha → off by default),
  the /coverage page, the box deploy.
- Fixture-only (built from real markup, never run against a logged-in account): every assisted-site apply flow,
  the Workday account + wizard path, the universal navigator's signup/OTP path, mailto apply, Gmail presets.
  **First use of each must be a dry run with the screenshots in `data/screenshots/<uid>/` reviewed.**

## Owner-side inputs still outstanding

Profile: email, phone, current/expected CTC, notice period. Settings → Mailbox: Gmail app password (unblocks
Workday + any OTP). Settings → Sites: cookie exports for Naukri / Instahyre / Cutshort / Wellfound (LinkedIn is
connected on the laptop, not on the box). An LLM key + `JOBAPPLY_LLM_BACKEND=live` when AI scoring is wanted.
Open port 8010 on the box.

## Known gaps / next candidates

Per-user billing and limits (needed before other people use it); scheduler tasks for assisted sites and Workday
(CLI entry points only today); `applied_today(site="external")` counts 0 for unknown hosts; a Workday host
reached via redirect inside the navigator pauses instead of re-queuing under workday; hosted deploy with HTTPS
and consent/terms; LinkedIn li_at rotation (re-login once, then reconnect).

## Conventions

Commits are plain, no attribution trailers. Never `git add -A` (data/ lives beside the code). Windows: every
`open()` passes `encoding="utf-8"`; run tools with `PYTHONUTF8=1`. Only the bundled headless Chromium works on
the dev laptop (headful spawn fails). One dashboard process at a time (SQLite lock). Tests: `uv run pytest -q
-p no:cacheprovider` (~5 min, Playwright fixtures included), `uv run ruff check src tests`.
