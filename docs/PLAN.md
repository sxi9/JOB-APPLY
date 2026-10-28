# JOB-APPLY plan

## Goal

A system that finds recently posted jobs across many sites, keeps every user's application data filled in once, and applies automatically (or with one-click approval) to many companies, with AI used both for deciding and tailoring (outbound) and for reading and reacting to replies (inbound). Multiple users, each isolated.

## Caveat on auto-apply

LinkedIn, Indeed, Naukri and Glassdoor ban accounts for bot applying and use CAPTCHAs and fingerprinting. The reliable path is fully automated on company ATS pages (Greenhouse, Lever, Ashby, Workable, SmartRecruiters), which have no login and public APIs, and rate-limited assisted mode on LinkedIn and Naukri using the user's real logged-in browser profile. Final submit is configurable per site: auto or human-approved.

## Sources

See [SOURCES.md](SOURCES.md).

## Pipeline

1. Collect: scheduler runs every 30 to 60 min per source. Each adapter returns a normalized job record.
2. Dedupe: same job appears on LinkedIn, Indeed and the ATS. Hash on company + title + location, fuzzy-match descriptions, prefer the ATS link.
3. Filter per user: posted within N hours, title keywords include/exclude, location/remote, seniority, salary floor, company blocklist, already-applied check.
4. AI score: Claude API gets the user's profile plus the job description, returns match score, missing skills, one-line reason. Threshold decides auto-apply vs review queue.
5. Tailor: per job, generate a resume variant (JSON Resume master rendered to PDF via template) and a short cover letter, cached per company.
6. Apply: adapter per ATS, generic LLM form filler for unknown forms. See [FORM_PROFILES.md](FORM_PROFILES.md).
7. Track: applications table with status, timestamps, screenshot of the submitted form, answers sent. Dashboard shows new / queued / applied / replied. See [DASHBOARD.md](DASHBOARD.md).

## Auto-apply layers

- Profile vault per user: contact, links, work authorization, visa, notice period, current and expected CTC, years per skill, EEO answers, education, references, canned answers. Resume files per role type.
- Known ATS forms: Greenhouse, Lever, Ashby, Workable, SmartRecruiters have stable field names. Playwright fills deterministically, uploads the PDF, submits.
- Unknown forms (AI way 1): Playwright dumps the accessibility tree, Claude maps each field to a vault key or writes a free-text answer, we execute the fill plan and save the mapping as a learned profile. Low confidence or CAPTCHA pauses and notifies.
- LinkedIn Easy Apply / Naukri: persistent logged-in Chrome profile, headful, hard cap (15 to 25 per day), random delays. Naukri profile refreshed daily to rank higher in recruiter search.
- AI way 2 (inbound): poll the user's mailbox, classify recruiter replies, rejections, assessments and interview requests, draft responses, update the tracker, forward invites to WhatsApp or real email.
- Safety: screenshot and answers stored for every submission, global dry-run mode, per-site daily caps, kill switch.

## Mailbox, accounts and OTP

Each user provides a dedicated mailbox (Gmail app password, or a custom domain on Zoho/Fastmail so `you+company@domain` tags which site leaked or spammed).

- Account creation: bot signs up on boards and ATS tenants with that mailbox, reads the verification link or OTP from IMAP, completes it.
- OTP / 2FA on login: bot polls IMAP for mail from that sender, extracts the 4 to 8 digit code or link, types it. Virtual SMS number optional later.
- Inbound: every reply lands in the inbox for the classifier.
- Outbound: cold applications and follow-ups go out over SMTP from the same mailbox with templates from the vault. Recruiters reply to this address, so it must be one the user keeps.

Credential storage: encrypted file (age or sops) or `.env` on the server, never in the repo. Browser sessions: Playwright persistent contexts, one Chrome profile per user per site, so logins are reused and OTP prompts are rare. Session health check before each run, re-auth through the OTP flow only when needed.

The maintainers never type a user's real passwords into live sites manually; the bot does it from the vault the user fills in.

## Multi-user design

- Users table. Each user: own profile vault, own credential entries per site, own mailbox, own browser profiles. Nothing shared between users.
- Shared layer: collectors run once for everyone. Jobs land in one table. Each user has own filters, scores, queue and applications pointing at shared job rows, so scraping load stays flat.
- Per-user filters: roles, keywords, locations, remote, salary, seniority, blocklist, daily apply cap, auto-submit on/off per site.
- Per-user AI: scoring and tailoring use that user's profile. Own Claude API key or a shared one with usage tracking.
- Isolation for applying: one Playwright profile directory per user per site, separate IMAP connection per user, rate limits per user account.
- Dashboard: login per user, admin role sees everyone.
- Onboarding: profile form, resume upload, site logins and mailbox, filters, site toggles. No code changes.

## Stack

Python 3.11 with uv, httpx, Playwright, SQLite via SQLModel (Postgres later if needed), APScheduler, FastAPI + HTMX dashboard, Claude API, python-jobspy. systemd services on a Linux box, dashboard behind Tailscale.

## Build phases

1. Core and collectors: repo scaffold, database (users / jobs / applications), adapters for ATS APIs and aggregator APIs, dedupe, per-user filters, basic dashboard. Done when new jobs show per user with correct posted times.
2. AI layer: scoring, resume variants, cover letters, review queue. Done when every new job has a score and a tailored resume ready.
3. ATS auto-apply: Playwright fillers for Greenhouse, Lever, Ashby, Workable, SmartRecruiters, screenshots, dry-run, daily caps. Done when a filtered job gets applied with no human touch.
4. Accounts and OTP: credential vault, IMAP reader, auto signup on Workday and similar, session health checks. Done when a fresh user gets accounts created and logged in automatically.
5. Assisted sites: LinkedIn Easy Apply with caps, Naukri one-click and daily profile refresh, Instahyre, Cutshort. Done when these run inside limits without logouts.
6. Generic form filler: LLM reads unknown forms, learned profiles, pause on low confidence or CAPTCHA with WhatsApp notify.
7. Inbound tracking: classify replies, update status, forward invites, scheduled follow-ups.
8. Deploy: systemd, Tailscale, database and vault backups.

Sequential estimate: 8 to 12 working days.

## Parallel build schedule (multiple agents)

| Wave | Agents at once | Time |
|---|---|---|
| 0 | Scaffold, schema, user model, config and vault format, shared interfaces | 1 to 2 hours |
| 1 | A: ATS API collectors · B: aggregator collectors · C: dashboard with login, multi-user, jobs page · D: dedupe and filter engine · E: credential vault, IMAP OTP reader, SMTP sender | half a day |
| 2 | A: fillers for Greenhouse, Lever · B: fillers for Ashby, Workable, SmartRecruiters · C: AI scoring, resume variants, cover letters · D: queue and applications pages, screenshots · E: generic AI form mapper with learned profiles · F: inbox classifier, WhatsApp alerts | 1 day |
| 3 | A: LinkedIn Easy Apply · B: Naukri and Instahyre · C: Workday signup flow · D: deploy, systemd, Tailscale, backups · E: end-to-end dry-run tests | 1 day |
| 4 | Integration, fixes from dry-run reviews, flip sites to auto-submit | half to 1 day |

Total around 3 to 4 working days. Each agent works in its own worktree on its own module, then merged.

Cannot be rushed: user inputs (resume, answers vault, logins, mailbox), human dry-run review before any site goes live, cautious real-account testing on LinkedIn and Naukri.

## Inputs needed from each user

1. Target roles, keywords, locations or remote, salary floor.
2. Priority sites and a target company list for direct ATS polling.
3. Resume file plus standard answers: notice period, CTC, work authorization, links.
4. Dedicated mailbox provider and credentials.
5. Site logins for assisted sites.
