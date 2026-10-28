# Build status

Tracks the cloud build from [CLOUD_BUILD.md](CLOUD_BUILD.md). Updated after each step.

## 1. Scaffold — done

- uv project, Python 3.11 (`.python-version`), package `jobapply` under `src/`.
- Settings via pydantic-settings, prefix `JOBAPPLY_`, read from `.env` (see `.env.example`). Dry run defaults on.
- Structured logging (`jobapply.logs`): key=value or JSON lines (`JOBAPPLY_LOG_JSON=true`).
- CLI `jobapply` with `collect`, `score`, `apply --dry-run`, `serve`.
- Stubbed: all commands log "not implemented yet" until their steps land.

Run: `uv sync && uv run jobapply --help && uv run pytest`

## 2. Database — done

- SQLModel models in `jobapply.models`. Shared: `jobs`, `job_sources`, `form_profiles`, `collector_runs`, `app_settings`. Per user: `users`, `user_sites`, `applications`, `application_events`, `answers_bank`, `credentials`, `mail_messages`, `llm_usage`.
- `applications` is the per-user-per-job row for the whole lifecycle (new → scored/queued → dry_run/applied → replied/interview/offer/rejected), holding score, tailored resume, cover letter, answers sent and screenshot.
- `jobapply.repo.UserScope` is the only way to read per-user tables; every query filters on `user_id`.
- Credentials are Fernet-encrypted (`jobapply.vault`), key from `JOBAPPLY_VAULT_KEY`; `jobapply vault-key` prints a new one.
- Alembic migrations in `src/jobapply/migrations`, applied with `jobapply db upgrade`. SQLite by default. Datetimes stored as UTC, returned timezone-aware.

## 3. Collectors — done

- `jobapply.collectors.base`: `NormalizedJob` (the record from SOURCES.md), `BaseCollector`, HTML/date/salary/remote helpers.
- ATS (one instance per company, public JSON, no auth): Greenhouse, Lever, Ashby, Workable, SmartRecruiters (`with_details=True` fetches descriptions).
- Feeds (no key): Remotive, RemoteOK, Jobicy, Arbeitnow, The Muse, HN Who is hiring (Algolia API, header line parsed into company / role / location).
- python-jobspy adapter for Indeed, LinkedIn guest search, Glassdoor, ZipRecruiter (optional extra `uv sync --extra jobspy`; enable sites in `config/searches.yaml`).
- Keyed: Adzuna (`JOBAPPLY_ADZUNA_APP_ID/KEY`) and JSearch (`JOBAPPLY_RAPIDAPI_KEY`), enabled only when keys are set.
- ATS detector (`collectors.detector`) classifies careers URLs, following redirects and embeds; `jobapply detect-companies` writes ats/slug back to `config/companies.yaml`.
- Fixtures in `tests/fixtures/collectors/` are trimmed real responses recorded 2026-10-28, except Adzuna and JSearch, which are synthetic, built from the documented response shapes because no keys were available.
- Live smoke run from the sandbox returned jobs from every ATS and feed adapter.
- Stubbed or untested live: jobspy (tested with a fake scraper only), Adzuna, JSearch. Workday and Naukri/Instahyre/Cutshort/Wellfound collectors are local-build items.

## 4. Dedupe, filters, scheduler — done

- `pipeline.dedupe.store_jobs`: upsert per (source, source_id); exact match on hash of normalized company + title + location; fuzzy pass over the same company's jobs from the last 30 days (title ratio ≥ 92, description token-set ≥ 85). The canonical row switches to the ATS link and ATS posting time when an ATS source appears. Same-title, same-location postings at one company merge on purpose, so a user never applies twice to the same role.
- `pipeline.filters`: `UserFilters` (roles, include/exclude keywords, locations incl. "remote", remote only, salary floor, seniority, company blocklist, sources, max age in hours) stored in `users.filters`. `assign_jobs` creates `applications` rows with status `new` per user and never re-adds a job the user already has.
- `pipeline.collect.run_all`: runs every collector, records `collector_runs` (failures recorded, not raised), assigns new jobs. Each active user's `filters.roles` are added to search queries.
- Scheduler (`jobapply schedule [-g collector|worker]`): APScheduler with jitter, coalescing, one instance per task. The collect interval comes from `JOBAPPLY_COLLECT_INTERVAL_MINUTES` or the admin setting.
- Global switches (`jobapply.switches`): kill switch and dry run; env and admin toggle combine to the safe side.
- CLI: `jobapply collect [-s source]`, `jobapply refilter`.
- Live check: `jobapply collect -s greenhouse -s ashby -s remotive` stored 882 jobs from 900 fetched.

## 5. Dashboard — done

- FastAPI + Jinja2 + HTMX (vendored in `web/static/htmx.min.js`, no CDN), dark theme, phone layout, served with `jobapply serve`.
- Login with username/password (PBKDF2), signed session cookie, CSRF on every POST, roles admin/user. Admin can "View as" any user; every page then shows that user's data with a banner.
- Pages from DASHBOARD.md: Home (stat tiles, funnel, activity feed with screenshot thumbnails, collector health), Jobs (filters: posted within, source, score, location, remote, salary, company, status, text search; HTMX live filtering; detail with AI reason, missing skills, resume variant, Apply now / Queue / Skip / Blocklist), Queue (answers editable next to the cover letter, dry-run screenshot, Approve / Save / Reject), Applications (timeline, screenshot, answers sent, follow-up date, reply thread), Inbox (categories, AI draft reply), Profile (answers vault, skills with years, JSON Resume master, resume uploads, answer bank), Settings (filters, sites with enable/auto-submit/cap/re-login, encrypted credentials, AI, notifications), Admin (users, caps, usage, kill switch, dry-run switch, collector interval, run collectors now, collector log).
- `jobapply seed` creates `admin / admin1234` and `demo / demo1234` with 40 demo jobs, applications in every status, mails and collector runs. No API key needed.
- `jobapply create-user NAME [--admin]` for real users.
- Tests render every page as user and admin, check isolation (another user's rows 404), CSRF, actions, settings and profile saves. Screenshots were reviewed at 1280px and 390px with Playwright.
- Stubbed: Re-login button only records the request (the OTP flow is on the apply worker), inbox "send reply" and "forward" wait for the mail worker (step 8), resume variant files are generated in step 6.

## 6. AI layer — done

- `jobapply.ai.base.LLM` is the single interface (`complete(LLMRequest) -> LLMResult`, Pydantic schemas for structured tasks). Callers never know which provider answered; `llm_usage` records provider:model and tokens per call per user.
- Providers (`jobapply.ai.providers.CATALOG`): Anthropic Claude (official SDK, structured outputs, prompt caching on the per-user system prompt, server-side refusal fallbacks `fallbacks="default"`), OpenAI, Google Gemini, OpenRouter, Groq, Together, Mistral, DeepSeek, xAI, Ollama (local). Everything except Claude goes through two small httpx adapters (`openai_compat.py`, `gemini.py`); OpenAI-compatible providers get `response_format=json_schema`, falling back to `json_object` and then prompt-only JSON when a provider rejects it.
- Failover (`FailoverLLM`): providers are tried in order. Rate limit, out of credits, auth failure, 5xx or network error puts that provider in cooldown (`JOBAPPLY_LLM_COOLDOWN_MINUTES`, 6x longer for credit/auth problems) and the next one answers; a refusal or a bad request just skips to the next. Cooldowns are keyed by provider + key, so one user's exhausted key does not block another's. The Admin page shows provider state and 30-day usage per provider:model.
- Configuration: `JOBAPPLY_LLM_BACKEND=fake|live`, `JOBAPPLY_LLM_PROVIDERS` order, `JOBAPPLY_<PROVIDER>_API_KEY/_MODEL/_BASE_URL`. Each user can set their own order, models and keys on Settings → AI (keys encrypted in the vault); their keys win over the server's.
- Tasks (`jobapply.ai.service`): `score_job` (0–100, one-line reason, matched/missing skills), `write_cover_letter` (tone from preferences), `tailor_resume` (JSON Resume variant: reorder/rephrase only, contact details never sent to the model) rendered to PDF with fpdf2 (DejaVu/Liberation for Unicode) and cached per user+company for `JOBAPPLY_RESUME_CACHE_DAYS`, `answer_question` (answer bank first, AI draft stored unapproved for the user to review), generic `structured()` for later modules.
- `FakeLLM`: deterministic skill-overlap scoring, template letters, reorder-based tailoring, profile-based answers, keyword mail classification; tests can queue canned responses. `jobapply score` on the seeded demo works end to end with it.
- Pipeline: `jobapply score [-u user] [-n limit]` and the `score` scheduler task (worker group, every 15 min) score `new` applications; score ≥ the user's threshold → resume variant + cover letter prepared, status `queued`; below → `scored`. A retryable failure stops the run and leaves the rest `new` for next time.
- Not verified live: no provider keys were available in the cloud sandbox, so all providers are tested against stub clients / recorded response shapes. First local run: set one key, `JOBAPPLY_LLM_BACKEND=live`, `jobapply score -n 3`, check the Admin provider table.

## 7. Form engine — done

- `jobapply.forms.profiles`: YAML profiles in `config/form_profiles/` map selectors (CSS or `label=<text>`) to vault keys (`profile.*`, `derived.*`, `answers.<question>`, `const:<value>`, `skip`). Built in: Greenhouse, Lever, Ashby (selectors recorded from live forms on 2026-10-28), Workable and SmartRecruiters (from their standard field names; the live pages were captcha-gated from the sandbox, so review those two on the first local dry run).
- `jobapply.forms.fields`: in-page extractor returning every control with a stable selector, label, group (for radio/checkbox), options, required and hidden flags; captcha detection (reCAPTCHA, hCaptcha, Turnstile, Arkose).
- `jobapply.forms.fingerprint`: hash of field types + labels with generated ids normalised, keyed with the domain for learned profiles.
- `jobapply.forms.mapper`: AI mapper over the field list (never sees candidate data) with a closed key vocabulary; low-confidence and needs-human entries are not saved; the result is stored in `form_profiles` as a learned profile shared by all users.
- `jobapply.forms.runner.FormRunner`: builtin profile by URL → learned by fingerprint → AI mapper when fewer than half the profile's selectors still match (site changed its form). Fills text, selects, radio/checkbox groups (option picked by label, falling back to the answer source with the options), uploads the resume, answers custom questions from the answer bank then the LLM (low confidence → flagged), screenshots, and never clicks submit unless the pipeline passes `submit=True`. Pauses on captcha, needs-human questions, or required fields left empty.
- `jobapply.pipeline.apply`: `jobapply apply [--dry-run|--submit] [-u user] [-n limit]` and the `apply` scheduler task. Picks approved then queued applications on enabled sites, honours the kill switch, the global dry-run switch, the user's daily cap and per-site caps. A real submit needs `--submit` (or the scheduler) AND the global dry-run switch off AND the site's auto-submit on. Outcomes: `dry_run` (review on the Queue page with the filled answers and screenshot), `applied`, `paused` (WhatsApp/notifier alert), `failed`.
- Tests run the real runner in headless Chromium against HTML fixtures built from the recorded field maps (`tests/fixtures/forms/`), plus unit tests for keys, selectors, fingerprints, the mapper, status transitions, caps and switches. In this sandbox Playwright needs `JOBAPPLY_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium`; locally `uv run playwright install chromium` is enough.
- Not done here: dry runs against live ATS pages (blocked by the sandbox's policy on interacting with third-party sites; it is the first item of the local checklist). `FakeLLM` includes a heuristic form mapper, so the whole apply path can be exercised with `JOBAPPLY_LLM_BACKEND=fake`.

## 8. Mail — done

- `jobapply.mail.imap.ImapReader`: standard-library IMAP (SSL) with the connection injectable, so tests run against an in-process fake server. Parses text and HTML bodies, polls with `wait_for`.
- `jobapply.mail.otp`: `extract_code` (4–8 digit codes near code/OTP/verification wording), `extract_links` (verification/confirmation links, marketing links skipped), `wait_for_code(reader, sender=...)` and `wait_for_link(...)` for signup and 2FA flows. The site fillers call these during account creation and re-login (local build).
- `jobapply.mail.smtp.SmtpSender`: STARTTLS or SSL, threading headers for replies, attachments.
- `jobapply.mail.classify`: rules for the obvious cases (verification, spam, acknowledgements, rejections, interviews, assessments) and an LLM pass (`classify` task) that also extracts company/title and drafts a reply when one is expected. `link_application` matches mail to an application by sender domain, named company/title, or reply threading. Statuses never move backwards.
- `jobapply.mail.inbox`: `sync_inbox` stores new mail in `mail_messages` (deduped by Message-ID), links it, updates the application (interview / assessment / rejected / replied), clears the follow-up when the recruiter wrote, notifies, and forwards interviews and assessments to the user's forward address. `send_reply` answers from the dashboard. `send_follow_ups` chases applications quiet for 7 days: a mail to the recruiter when one exists, otherwise an event plus a reminder; at most 2 chasers.
- Mailbox credentials: Settings → Credentials, site `mailbox`, kind `imap` (host, port, app password) and optionally `smtp`; without an smtp entry the imap login is reused on `smtp.<host>`:587.
- CLI `jobapply inbox [-u user]`, `jobapply follow-ups`; scheduler tasks `inbox` (every 10 min) and `follow_ups` (daily 09:30 UTC) in the worker group. Inbox page: Send reply and Forward buttons (enabled once SMTP and a forward address are set).
- Not exercised live: no real mailbox in the sandbox. Everything runs on recorded message shapes and the fake IMAP/SMTP servers.

## 9. Notifier — done

- `jobapply.notify`: `Notifier` protocol, `LogNotifier`, `WhatsAppNotifier` (`POST {JOBAPPLY_WHATSAPP_BASE_URL}/send` with `{"to", "message"}` and a bearer `JOBAPPLY_WHATSAPP_TOKEN`), `MultiNotifier` fan-out. `build_notifier()` picks the WhatsApp backend when the base URL is set; `notify(session, user, kind, message)` honours each user's alert types (Settings → Notifications) and their WhatsApp number.
- Alert kinds in use: `paused` (apply run needs a human), `interview` and `assessment` (inbox), `recruiter_question`, `daily_summary` (`jobapply summary`, scheduled 15:00 UTC).
- Tested against a mock HTTP transport; the bridge itself is stubbed.

## 10. Deployment and CI — done

- `deploy/systemd/`: `jobapply-dashboard` (`jobapply serve`), `jobapply-collector` (`jobapply schedule -g collector`), `jobapply-worker` (`jobapply schedule -g worker`: score, apply, inbox, follow-ups, daily summary), `jobapply-backup.service` + nightly timer. All run as the `jobapply` user from `/opt/jobapply` with `EnvironmentFile=/opt/jobapply/.env`.
- `deploy/deploy.sh`: idempotent Ubuntu deploy (user, uv, venv with the jobspy extra, Chromium and its deps, `.env` seeded with fresh secret and vault keys on first run, migrations, units, restart). `deploy/backup.sh`: consistent SQLite `.backup` plus `data/`, `.env` and `vault/` into a rotated tar.gz.
- `Dockerfile` (python 3.11 slim, uv, Chromium) and `docker-compose.yml` with dashboard / collector / worker services sharing `./data`; `docker compose --profile setup run migrate` for migrations.
- `.github/workflows/ci.yml`: uv sync, ruff, pytest with Chromium (the Playwright form tests run in CI), migrations + seed on a fresh database, Docker image build and smoke.
- Not run here: the deploy script and the image build need a real box / Docker daemon. `bash -n` passes on both scripts.

## What to do first locally

1. `git pull`, `cp .env.example .env`, set `JOBAPPLY_SECRET_KEY`, `JOBAPPLY_VAULT_KEY` (`uv run jobapply vault-key`), one LLM key and `JOBAPPLY_LLM_BACKEND=live`.
2. `uv sync --extra jobspy && uv run playwright install chromium && uv run jobapply db upgrade && uv run jobapply create-user you --admin && uv run jobapply serve`.
3. Fill Profile and Settings (filters, sites, mailbox credentials under site `mailbox`), add target companies to `config/companies.yaml`, then `uv run jobapply collect && uv run jobapply score`.
4. `uv run jobapply apply --dry-run -n 3`, review the Queue page screenshots, fix any selector in `config/form_profiles/` that drifted, flip a site's auto-submit on only after clean dry runs, then switch the Admin dry-run toggle off.
5. Then the local-only items from docs/CLOUD_BUILD.md: assisted sites, Workday signup with real OTP, deploy with `deploy/deploy.sh`.
