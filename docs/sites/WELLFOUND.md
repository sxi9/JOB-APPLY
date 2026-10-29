# Wellfound (wellfound.com, ex-AngelList Talent)

Package: `src/jobapply/sites/wellfound/` (`search.py`, `session.py`, `apply.py`). Tests: `tests/test_wellfound_*.py`.
Same layout, logging and safety gates as the LinkedIn package. Research date: 2026-10-29.

## Public vs login-only (live-verified 2026-10-29, logged out, plain `httpx` with Chrome headers)

| Surface | Result | Used for |
|---|---|---|
| `GET /jobs` | 200, Next.js page, `__NEXT_DATA__` with featured `JobListing`/`Startup` entries | session check URL (`HOME_URL`); parser handles the shape |
| `GET /role/<role-slug>` (e.g. `/role/python-developer`) | 200, `JobListingSearchResult` + `StartupResult` in `__NEXT_DATA__`, 783 jobs / 24 pages | collector |
| `GET /role/l/<role-slug>/<location-slug>` (`/role/l/python-developer/india`) | 200, 197 jobs / 8 pages, `?page=2` works | collector (query + location) |
| `GET /role/r/<role-slug>` | 200, remote-only results (`"remote":true` in the query key) | collector (`remote_only` or location "Remote") |
| `GET /jobs/<id>-<slug>` | 200, server-rendered React page, JSON-LD `JobPosting`, `#job-description`, "Apply Now" button | optional detail fetch; the apply page |
| `GET /login` | 200, `form#new_user` with `input#user_email[name=username]` | classify_page markers only; never filled |
| `GET /company/<slug>/jobs` | **403**, Cloudflare `cf-mitigated: challenge`, title "Security Check \| Wellfound" | not used |
| GraphQL (`/graphql`) | not called (the SSR pages already embed the Apollo cache) | not used |

Eight requests in total were made, 3-6 s apart. Only the company page was challenged; every role page, the job
page and `/login` answered 200 on the first try. Logged-in state could not be verified live (no credentials, no
session); everything marked *inferred* below needs a check against a real session.

Logged-out markers (verified): the Next.js viewer has `"currentUser": null`, `"canApplyToJobs": false`; the job
page header renders `<button onclick="window.location.href='/login?after_sign_in=...'">Log In</button>` and the Save
button points at `/jobs/signup?after_action=save_job...`; "Apply Now" links to `?autoOpenApplication=true`.

## Collector (`search.WellfoundCollector`)

```python
WellfoundCollector(queries, location="", hours_old=24, max_results=50, max_requests=40, user_id=None,
                   remote_only=False, with_details=False, max_details=20, max_pages=3, delay=2.0,
                   client=None, sleep=time.sleep, skip_if=None, on_rate_limited=None)
collector.collect() -> list[NormalizedJob]   # source="wellfound", ats_type="wellfound", apply_url = job page
```

- Query to role slug: `role_slug("Python Developer") == "python-developer"`, with a few aliases
  (`sde`, `backend developer`, `ml engineer`, ...) in `ROLE_ALIASES`. Wellfound's role taxonomy is fixed: an
  unknown slug answers 404, which the collector logs and skips (not a block). Location to slug:
  `location_slug("Bengaluru") == "bangalore"` (`LOCATION_ALIASES`), "Remote" switches to the `/role/r/` page.
- Each page carries 20 startups with their highlighted listings (`PAGE_SIZE`); `pageCount` drives pagination,
  `max_pages` caps it (default 3 pages per query).
- Fields parsed per listing: `title`, `company` (startup name), `location` (`locationNames`, plus accepted remote
  locations), `remote` (`remote` / `remoteConfig.kind`), `posted_at` (`liveStartAt`, epoch seconds; matches the
  JSON-LD `datePosted` on the job page), `description` (markdown marks stripped), `compensation` to
  `salary_min/max/currency` (best effort via `parse_salary`; INR lakh strings keep the raw text), raw: `slug`,
  `job_type`, `years_experience_min/max`, `remote_kind`, `accepted_remote_locations`, `primary_role`,
  `ats_source`, `auto_posted`, `company_id/slug/size/pitch`, `badges`.
- `hours_old` filters on `liveStartAt` after collection (0 = no filter). The role pages are relevance-sorted, so a
  tight window can legitimately return few jobs; the log line says how many were dropped.
- `with_details=True` fetches the job page (JSON-LD) for up to `max_details` jobs: longer description,
  `employment_type`, `direct_apply`, `apply_available`, skills chips.
- `user_id` is accepted for interface uniformity only: the public surface needs no login, so the persistent profile
  is never opened by the collector (it only changes `key` to `wellfound:public:u<id>`).

### Rate limiting / blocks

Observed: role and job pages served freely at ~1 request per 4 s; `/company/.../jobs` challenged immediately.
The collector treats as a block and stops the run (keeping what it has): HTTP 403 (`cf-mitigated: challenge`
is logged as "cloudflare challenge") or 429 (`Retry-After` honoured), a 200 whose body is the Cloudflare
interstitial (`challenge-platform`, `_cf_chl_opt`, "Security Check"), and a redirect to `/login`, `/join` or
`/jobs/signup`. On a block it calls `on_rate_limited(hours)` once (default 2 h). The cross-run cooldown lives in
`app_settings` under `wellfound_guest_cooldown_until` (`set_guest_cooldown` doubles a still-active window, 48 h
cap; `guest_cooldown_active` also honours any user's challenge cooldown), exactly like
`linkedin_guest_cooldown_until`, so the registry can wire `skip_if` / `on_rate_limited` the same way.

## Session (`session.py`)

- `classify_page(page) -> "ok" | "login" | "challenge" | "unknown"` (never navigates). Order: captcha /
  Cloudflare markers (`detect_captcha`, `#challenge-form`, Turnstile iframe, titles "Security Check", "Just a
  moment"); login URL (`/login`, `/join`, `/jobs/signup`, ...) or `form#new_user`; the `__NEXT_DATA__` viewer
  (`currentUser` set = ok, null = login, decisive on `/jobs` and `/role/...`); *inferred* logged-in markers
  (`a[href='/logout']`, `[data-test='UserMenu']`, ...); the verified logged-out header buttons; challenge wording
  on a page that rendered no content. A job page with content but no decisive marker is `unknown` (apply.py then
  probes the Apply button).
- `WellfoundSession(context, user, db, llm=None, answers=None)`: `check(page)` opens `HOME_URL`
  (`https://wellfound.com/jobs`) and records the state on the user's `wellfound` site row. The stored
  `login_status` uses the dashboard vocabulary: `ok | expired | challenge | unknown` (`login` is stored as
  `expired`). A challenge sets a 6 h cooldown in `user.preferences["wellfound_cooldown_until"]`; `throttled()`
  reads it. `unknown` never overwrites a stored `ok`.
- `check_session(user_id, engine=None) -> str`: opens the persistent profile **headless**
  (`launch_persistent(pw, user_id, "wellfound", headless=True)`), runs `check`, closes the context.
- `import_cookies(user_id, path, engine=None) -> str`: `import_cookies_file(user_id, "wellfound", path,
  allowed=("wellfound.com", "angel.co"))`, then `check_session`. Returns the resulting state.

### How to log in (cookie export)

1. Log in to wellfound.com in your own browser.
2. Export cookies with a cookie-export extension as JSON (array of `{name, value, domain, path, expirationDate,
   secure, httpOnly, sameSite}`; the `{cookies: [...]}` wrapper is accepted too). Only `wellfound.com` /
   `angel.co` cookies are kept. Keep the file outside the repo; it is read once and never logged.
3. `import_cookies(user_id, "<path>.json")` (the wiring agent exposes it on the CLI): the profile under
   `data/browser-profiles/<user_id>/wellfound` is now logged in; the return value should be `ok`.
4. Re-run `check_session` any time; `expired` means export again, `challenge` means wait for the cooldown.

## Apply (`apply.apply_job`)

```python
apply_job(session: WellfoundSession, application, *, dry_run: bool, auto_submit: bool,
          job=None, selectors=None, screenshot_path=None, pause=None) -> dict
# {"status": "dry_run"|"applied"|"paused"|"failed"|"skipped", "reason", "screenshot", "url", "submitted",
#  "answers": {...}, "needs_human": [...], "unfilled_required": [...]}
as_fill_result(result) -> FillResult      # for pipeline.apply.record_outcome
pick_wellfound_applications(db, user, limit) -> [(Application, Job)]
```

Gates, in order, all before the browser opens: kill switch (`skipped`), `dry_run = dry_run or
global_dry_run(db)`, the user's `wellfound` site enabled, `throttled()` (challenge cooldown), active hours
(`JOBAPPLY_WELLFOUND_ACTIVE_HOURS` once settings grow it; until then the LinkedIn window), and, for a non-dry run,
the per-user `daily_cap` preference and the site's `daily_cap` (default 15, `users.SITES`). The final submit
happens only when `not dry_run and auto_submit and site.auto_submit`; everything else fills, screenshots under
`data/screenshots/<user_id>/<app_id>-<ts>-<rnd>.png`, closes the modal (Escape, then the close button) and returns
`dry_run` with the reason ("dry run: global dry-run on", "auto-submit off for wellfound ...").

Flow: open the job page; 403/429 on navigation, a captcha or a Cloudflare page -> `record_challenge()` (status
`challenge`, cooldown) and `paused`, never retried in the run; a login page -> `expired` and `paused` ("login
required: import cookies"); an "Applied" marker -> `failed` ("already applied"); no Apply button -> `failed`.
Then click Apply, wait for the note box, re-classify (a redirect to `/login` or a Turnstile inside the modal is
caught here), write the note (`application.message`, else `jobapply.ai.service.application_message`, which
uses the LLM when one is configured and the template otherwise; capped at `NOTE_MAX_CHARS` = 1000), answer any
other modal fields through the answer source (`session.answers`, else `forms.runner.AnswerSource`; low
confidence -> `needs_human`, a required field nobody can answer -> `paused: required fields left empty`),
upload the resume to a file input when one is present, screenshot. A submit click is verified by the success
marker / the modal closing; an inline error -> `failed`; a modal that stays open -> `paused` (verify by hand).

### Selectors relied on

Verified logged out: `[data-test='JobApplicationApplyButton']` (role-page cards) and the job page's "Apply Now"
button; `[data-test='JobListing']`, `[data-test='JobListingCompanyHeader']`, `#job-description`, JSON-LD
`JobPosting`; the header Log In / Sign Up buttons; `form#new_user` on `/login`.

*Inferred, unverified* (no logged-in session was available): everything inside the apply modal, held in
`apply.DEFAULT_SELECTORS` (`modal`, `note`, `submit`, `close`, `success`, `error`, `applied_marker`) with
deliberately broad fallbacks (`[role='dialog'] textarea`, `[role='dialog'] button[type='submit']`, ...). Override
without a code change by creating `config/sites/wellfound.yaml` (`selectors:` map, same idea as
`config/sites/linkedin.yaml`; the wiring agent owns `config/sites/`).

## Known gaps

- `config/form_profiles/wellfound.yaml` was deliberately **not** created: `forms.profiles.load_builtin_profiles`
  loads every `*.yaml` there as an ATS profile and `tests/test_forms.py::test_builtin_profiles_load_and_detect`
  asserts the exact builtin set, which would break. The Wellfound apply is a modal driven by `apply.py`, not a
  `FormRunner` form; its selectors live in `DEFAULT_SELECTORS` plus the optional `config/sites/wellfound.yaml`.
- Logged-in markers and the apply-modal selectors are inferred (see above); first real run should be a dry run
  with the screenshot reviewed on the Queue page.
- Role slugs are Wellfound's fixed taxonomy; queries that do not map to one return nothing (404, logged).
- The role pages are relevance-sorted, not date-sorted; `hours_old` only filters, so a 24 h window over a slow
  role can be empty. `max_pages` (3) bounds the walk; raise it with care (each page is one request).
- No `wellfound_active_hours` / `wellfound_headless` settings exist yet (config.py is off limits here);
  `active_hours_spec()` falls back to `linkedin_active_hours`; the launcher is always headless.
- Not wired: registry, CLI, scheduler, `pipeline.apply.ASSISTED_SITES` (the wiring agent's job). `apply_job`
  returns a dict; `as_fill_result` converts it for `record_outcome`.
- The compensation parser is best effort for INR lakh notation (`₹15L – ₹25L`): currency is detected, the amounts
  are not scaled.
