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
