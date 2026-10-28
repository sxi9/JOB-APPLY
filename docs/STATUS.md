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
