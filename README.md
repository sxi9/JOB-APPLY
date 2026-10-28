# JOB-APPLY

Multi-user job scraper and auto-apply system. Collects fresh postings from many sources, scores them against each user's profile with an LLM, fills and submits application forms automatically (or with one-click approval), handles account signup and OTP through a dedicated mailbox, and tracks replies.

Status: cloud build in progress, see [docs/STATUS.md](docs/STATUS.md). Design in [docs/PLAN.md](docs/PLAN.md).

Quick start: `uv sync && cp .env.example .env && uv run jobapply seed && uv run jobapply serve` then log in as `demo / demo1234` (or `admin / admin1234`).

## What it does

1. Collects jobs every 30 to 60 minutes from ATS APIs (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday), aggregators (LinkedIn, Indeed, Naukri, Glassdoor, ZipRecruiter, Instahyre, Cutshort, Wellfound) and job APIs (Adzuna, JSearch, Remotive, RemoteOK, Jobicy, Arbeitnow).
2. Dedupes across sources and keeps the direct ATS link.
3. Filters per user: posted in last N hours, keywords, location, remote, salary, seniority, blocklist.
4. Scores each job against the user's profile with an LLM (Claude, OpenAI, Gemini, OpenRouter, Groq, Together, Mistral, DeepSeek, xAI or local Ollama, with automatic failover when one is rate limited or out of credits) and generates a tailored resume variant and cover letter.
5. Applies: deterministic form fillers for known ATS platforms, an AI form mapper for unknown forms that learns and saves the mapping, assisted mode with daily caps for LinkedIn and Naukri.
6. Handles signup, verification links and OTP codes by reading the user's dedicated mailbox over IMAP.
7. Tracks every application with screenshots, classifies inbound replies, forwards interview invites to WhatsApp or real email.

## Multi-user

Collectors run once and share one jobs table. Each user has their own profile vault, credentials, mailbox, browser sessions, filters, caps, queue and applications. Dashboard login per user, admin sees all.

## Stack

Python 3.11 (uv), httpx, Playwright, SQLite + SQLModel, APScheduler, FastAPI + HTMX dashboard, Claude API plus OpenAI-compatible and Gemini adapters with failover, python-jobspy. Runs as systemd services on a Linux box, dashboard behind Tailscale.

## Docs

- [docs/PLAN.md](docs/PLAN.md) full plan: pipeline, auto-apply layers, mailbox and OTP, multi-user design, build phases, parallel build schedule
- [docs/SOURCES.md](docs/SOURCES.md) source-by-source table with access method and apply method
- [docs/DASHBOARD.md](docs/DASHBOARD.md) page-by-page UI spec
- [docs/FORM_PROFILES.md](docs/FORM_PROFILES.md) how ATS forms are profiled, learned and verified
- [docs/CLOUD_BUILD.md](docs/CLOUD_BUILD.md) what to build in Claude Code on the web vs locally, with the cloud session brief
