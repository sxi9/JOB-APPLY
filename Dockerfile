# JOB-APPLY: one image, three roles (serve | schedule -g collector | schedule -g worker).
FROM python:3.11-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates sqlite3 rsync fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra jobspy
RUN uv run playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY . .
RUN uv sync --frozen --no-dev --extra jobspy
RUN useradd --system --create-home --uid 1000 jobapply && mkdir -p /app/data && chown -R jobapply:jobapply /app /ms-playwright
USER jobapply
ENV JOBAPPLY_HOST=0.0.0.0 JOBAPPLY_DATA_DIR=/app/data JOBAPPLY_DATABASE_URL=sqlite:////app/data/jobapply.db
EXPOSE 8000
VOLUME ["/app/data"]
ENTRYPOINT ["/app/.venv/bin/jobapply"]
CMD ["serve"]
