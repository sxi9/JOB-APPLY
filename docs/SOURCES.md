# Sources

Order is the order we add them. Rows 1 to 6 give exact posting timestamps, so "posted in the last 24 hours" is reliable there. Aggregators only give "x days ago" and are used for discovery; the direct ATS link is always preferred for applying.

| # | Source | Access | Login needed | Auto-apply |
|---|---|---|---|---|
| 1 | Greenhouse boards | public JSON API `boards-api.greenhouse.io/v1/boards/{company}/jobs` | no | yes, form filler |
| 2 | Lever | public JSON API `api.lever.co/v0/postings/{company}` | no | yes, form filler |
| 3 | Ashby | public JSON API | no | yes, form filler |
| 4 | Workable | public JSON API | no | yes |
| 5 | SmartRecruiters | public JSON API | no | yes |
| 6 | Workday tenants | JSON endpoint per tenant | account per tenant | yes, with signup + OTP |
| 7 | LinkedIn | guest search for listings, logged-in for Easy Apply | yes | capped, assisted |
| 8 | Naukri | search scrape + logged-in profile | yes | one-click apply, daily profile refresh |
| 9 | Indeed | python-jobspy | no for listing | redirect to ATS mostly |
| 10 | Glassdoor, ZipRecruiter | python-jobspy | no | redirect |
| 11 | Instahyre, Cutshort, Wellfound | Playwright scrape | yes | one-click where offered |
| 12 | Adzuna, JSearch (RapidAPI), Remotive, RemoteOK, Jobicy, Arbeitnow, The Muse, USAJobs | REST APIs | API key only | redirect to source |
| 13 | HN "Who is hiring", YC jobs | monthly thread parse, API | no | email apply |
| 14 | Company careers pages | ATS detector then poll the matching API | no | depends on ATS |

## Company discovery

Given a target company list (manual, or built from LinkedIn search, Crunchbase, "companies using X"), a detector hits each careers URL, follows redirects, and classifies the ATS (`boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com`, `myworkdayjobs.com`, ...). From then on that company is polled directly through the ATS API.

## Normalized job record

Every adapter returns: `title, company, location, remote, posted_at, url, apply_url, description, ats_type, salary_min, salary_max, currency, source, source_id, raw`.

## Dedupe

Hash on normalized company + title + location, then fuzzy-match descriptions. Multiple source rows link to one canonical job; the canonical apply URL is the ATS link when one exists.

## Breadth mode (all-rounder collection)

The shared job pool is meant to cover **every** sector in the role taxonomy, not just the handful of
companies in `config/companies.yaml`. Two things make the pool broad while each user still sees only
their own roles:

- **Breadth query set** — `jobapply.roles.search_queries(fields=None, per_field=3)` returns a deduped,
  representative set of job-title queries spanning all taxonomy fields (e.g. "software engineer",
  "soc analyst", "threat intelligence", "data engineer", "product manager", "devops engineer",
  "ux designer", ...). It is pure and deterministic.
- **Breadth mode** — when `config/searches.yaml` has `broad: true` (the default), the query-based
  collectors (the LinkedIn guest search and the keyed aggregators Adzuna / JSearch / jobspy) run over the
  whole-taxonomy query set instead of only the active users' `filters.roles`. To stay inside LinkedIn's
  guest rate limits, only a rotating slice of `broad_queries_per_run` queries (default 12) runs each
  collect; `pipeline.collect.broad_queries()` advances a stored offset in `app_settings`
  (`broad_query_offset`) so successive runs cycle through the full set and, over a few runs, every sector
  gets searched. Each active user's own roles are still appended (deduped) so nothing a user asked for is
  dropped, and per-user assignment (`pipeline.filters`) still filters each user to their roles. Net effect:
  a broad, user-independent pool + per-user filters = each user sees their roles while the DB covers
  everything. `run_all()` logs how many queries ran and how many jobs were added.

## Company list (direct-ATS tier)

`config/companies.yaml` is a curated multi-sector list of ~120 public ATS boards (Greenhouse, Lever,
Ashby, Workable, SmartRecruiters), grouped by sector (big tech, fintech, cybersecurity, SaaS / dev tools,
AI/ML, healthcare, retail, gaming, education, Indian tech, enterprise). Every slug was verified live
against its public JSON API on 2026-10-29 (board reachable with at least one posting). Add a
`careers_url`-only row and run `jobapply detect-companies` to resolve new boards.

## Stale / evergreen postings

Some ATS reqs (Lever especially) keep an original `createdAt` from years ago, which otherwise shows up as
e.g. "2906 d ago". We **never fake the date**: `posted_at` stays the real value. Instead
`collectors.ats.is_stale_posting()` flags any posting older than `JOBAPPLY_MAX_POSTING_AGE_DAYS`
(default 365) by setting `stale_posting: true` in the stored source `raw`, so the UI and scoring can
de-prioritise it. The default lines up with the sane default recency windows: the per-user filters
(`UserFilters.max_age_hours = 72`) and the jobs/coverage views already exclude postings this old, so an
evergreen req never lands in a user's queue by default.

## Coverage stats

`pipeline.collect.coverage_stats(session)` returns `{total, by_source, by_sector, last_24h, last_7d}` —
`by_sector` counts pool jobs per taxonomy field via `roles.match_title` (plus an `other` bucket; a title
may match several fields, so these can exceed `total`). The dashboard's Coverage page uses it when
present, and `jobapply coverage` prints it as JSON.
