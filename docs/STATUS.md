# Build status

Tracks the cloud build from [CLOUD_BUILD.md](CLOUD_BUILD.md). Updated after each step.

## 1. Scaffold — done

- uv project, Python 3.11 (`.python-version`), package `jobapply` under `src/`.
- Settings via pydantic-settings, prefix `JOBAPPLY_`, read from `.env` (see `.env.example`). Dry run defaults on.
- Structured logging (`jobapply.logs`): key=value or JSON lines (`JOBAPPLY_LOG_JSON=true`).
- CLI `jobapply` with `collect`, `score`, `apply --dry-run`, `serve`.
- Stubbed: all commands log "not implemented yet" until their steps land.

Run: `uv sync && uv run jobapply --help && uv run pytest`
