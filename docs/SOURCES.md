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
