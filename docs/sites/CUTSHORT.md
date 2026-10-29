# Cutshort (cutshort.io)

Package: `src/jobapply/sites/cutshort/` (`search.py`, `session.py`, `apply.py`), profile
`config/form_profiles/cutshort.yaml`, tests `tests/test_cutshort_*.py`. Researched live on 2026-10-29 with a
plain httpx client and one headless browser page, logged out; no account or credentials were used.

## Public vs login-only

| Area | Public (no login) | Login only |
| --- | --- | --- |
| Hub listing pages `/jobs/<hub-slug>` (`python-jobs`, `remote-python-jobs`, `python-jobs-in-pune`) | yes, Next.js shell hydrated from the JSON endpoint below | - |
| Job pages `/job/<Title-Company-<8 chars>>` | yes, server-rendered `__NEXT_DATA__` plus `JobPosting` ld+json | - |
| `GET /backend-api/webpage/jobs/<hub-slug>?page=N` | yes, 50 records per page, newest first | - |
| `GET /backend-api/webpage/job/<job-slug>` | yes, one record | - |
| `GET /backend-api/webpage/jobs?page=N` | yes, the 3 792 hub categories, 15 per page | - |
| Sitemaps `sitemap_jobs.xml` (44 749 job URLs, with `lastmod`), `sitemap_listings.xml` (20 972 hub slugs) | yes | - |
| Apply ("Apply to this job") | button renders logged out, opens the auth modal | **yes** (one click, sometimes a questionnaire) |
| Candidate area `/profile/...`, saved jobs, `authApplyUrl` (`/profile/view/j/<id>`) | redirects to `/?redirect_url=...` | **yes** |
| Login | `/login` is a 404; login is a modal ("Candidate login" in the unauth header, email + OTP / password / social) | - |

`robots.txt` disallows `/profile/`, `/view/j/`, `/apply/`, `/js/`; the public hub and job pages are allowed and
in the sitemaps. The site also publishes a REST API + MCP server at `/a/devdocs`, but that is the **employer**
(sourcing) API, not a candidate job-search API, so it is not used.

## Collector (`search.py`, logged out, live-verified)

`CutshortCollector(queries, location="", hours_old=24, max_results=50, max_requests=40, user_id=None, *,
remote_only=False, hub_slugs=None, delay=1.5, client=None, sleep=..., skip_if=None, on_rate_limited=None)`;
`name = source = "cutshort"`, `key = "cutshort:public"`, `collect() -> list[NormalizedJob]` (the pydantic
record every collector returns; `.model_dump()` gives the dict). `user_id` is accepted and unused.

Per record: `source="cutshort"`, `ats_type="cutshort"`, `url = apply_url = publicUrl`
(`https://cutshort.io/job/<slug>`), `source_id = _id` (24-hex Mongo id), `posted_at = jobFactSummary.postedDate`,
`description = sanitizedComment` as text plus a `Skills:` line, salary band only when `salaryRange.hideSalary`
is false (else `raw.salary_text`, usually "Best in industry"), `remote` from `remoteType` (`remote_only`,
`onsite`, ...) or the location text. `raw` carries `job_id`, `slug`, `hub_slug`, `auth_apply_url`, `skills`,
`exp_min/max`, `remote_type`, `role_types`, company type/size/stage, `posted_label`, `one_click_apply=True`.

Query to hub slug: a query that already is a hub slug is used verbatim; otherwise the slugified query is
tried as `<topic>-jobs`, with `-in-<city>` first when the location maps to one of the 15 city hubs
(`CITY_SLUGS`: Bengaluru -> `bangalore-bengaluru`, Gurgaon/Noida/Delhi -> `delhi-ncr-gurgaon-noida`, Kochi ->
`kochi-cochin`, ...), `remote-` prefixed for `remote_only`, and the generic tail / leading seniority peeled
off (`python developer` -> `python-developer-jobs` (400) -> `python-jobs` (200); `android-developer-jobs`
exists). At most 4 candidates per query; a miss is one cheap 400 (`errorCode = jobs_page_config_not_found`).

Pagination stops at `max_results`, a short page, an empty page, the request budget, or as soon as a page holds
a record older than `hours_old` (the hub is newest-first). Records are de-duplicated by id across queries.

Rate limiting observed: none. 20-odd requests at 1.5-2.5 s spacing from one IP all answered 200 with
`cache-control: public, max-age=14400` (Cloudflare, `cf-cache-status` hits), no `retry-after`, no cookies
needed. The 4 h CDN cache bounds freshness: the newest record on a hub page can be up to 4 h behind. The
collector still treats 429 / 403 / 503 as a block (stops, `on_rate_limited(hours)` with `Retry-After` honoured,
2 h default; `set_public_cooldown` / `public_cooldown_active` persist it in `app_settings` under
`cutshort_public_cooldown_until`, the wiring passes them as `on_rate_limited` / `skip_if`), and any other
4xx/5xx or non-JSON body ends the run without a cooldown.

## Session (`session.py`)

- `classify_page(page) -> "ok" | "login" | "challenge" | "unknown"`: challenge on a `/cdn-cgi/challenge` URL,
  a visible captcha, a Cloudflare title ("Just a moment...", "Attention required") or OTP wording; login on
  the `/?redirect_url=` home page or the unauth header / auth modal (`button:has-text("Candidate login")`,
  `input[type=password]`, `[id*="auth-modal"]`, `button:has-text("Get started")`); ok inside `/profile/`
  with the candidate shell (`a[href*="/profile/candidate-dashboard"]`, `#auth-page-wrapper`, a logout link)
  or any rendered content and no login marker; otherwise unknown.
- `CutshortSession(context, user, db).check(page)` opens `https://cutshort.io/profile/all-jobs`, polls until
  decisive, stores `user_sites.login_status` (`ok`, `expired` for login, `challenge` plus a 6 h cooldown in
  `users.preferences["cutshort_cooldown_until"]`; `unknown` never downgrades a stored `ok`).
- `check_session(user_id) -> str` and `import_cookies(user_id, path) -> str` (module-level, uniform interface)
  open the persistent profile `data/browser-profiles/<user_id>/cutshort` **headless** through
  `sites.browser.launch_persistent` / `import_cookies_file(..., allowed=("cutshort.io",))`.
- `throttled(user, site)` is the cooldown gate the apply path honours.

### How to log in (cookie export)

1. Log in to cutshort.io in any browser (email OTP, password or Google).
2. Export the cookies for `cutshort.io` with a cookie-export extension as JSON (the common
   `[{name, value, domain, path, expirationDate, secure, httpOnly, sameSite}]` list; a `{"cookies": [...]}`
   wrapper is fine). Only cookies whose host ends in `cutshort.io` are imported; values are never logged.
3. `import_cookies(user_id, "path/to/export.json")` loads them into the profile and runs `check_session`;
   `ok` means the candidate area opened without a redirect. The cookie names and lifetime of the session are
   not known (nobody was logged in during research); re-export when `check_session` reports `login`.

## Apply (`apply.py`, fixture-only)

`apply_job(session, application, *, dry_run, auto_submit) -> {"status", "screenshot", "reason", "result"}`
with the pipeline vocabulary `applied | dry_run | paused | failed | skipped`. `session` is a
`CutshortSession` (open context + DB session, as `apply_cutshort_user` builds it) or a plain DB `Session`
(then the profile is opened headless for this one job). `apply_cutshort_user(pw, session, user, llm, limit,
dry_run)` and `apply_cutshort(username, limit, dry_run, engine)` mirror `linkedin.easy_apply`;
`pick_cutshort_applications` takes approved-then-queued applications whose job has `ats_type == "cutshort"`.

Gates, each checked inside `apply_job` too: kill switch (`skipped`); global dry-run (`settings.dry_run` or
the admin toggle) forces `submit=False`; per-site `auto_submit` must be on; per-user and per-site daily caps
(`users.py` lists cutshort at 15); `throttled` cooldown; active hours (`JOBAPPLY_CUTSHORT_ACTIVE_HOURS`,
falling back to the LinkedIn window); a 403/429/503, captcha, Cloudflare/OTP page or the login redirect
pauses with a screenshot under `data/screenshots/<user_id>/` and is never retried in the same run (a
challenge also records `login_status=challenge` and the cooldown).

Flow with `submit=True`: open the public job page in the profile, verify the session (login marker ->
`paused "cutshort login needed"`), find `button:has-text("Apply to this job")` (an `Applied` button ->
`failed "already applied"`), click it, then either a success marker (one-click apply -> `applied`) or a
questionnaire dialog: fields are mapped by the YAML `label=` map (notice period, CTC, experience, contact,
links, resume), then the LinkedIn label heuristics and skill-years, then the answer bank / LLM
(`AnswerSource`); a low-confidence or missing answer pauses the run, closes the dialog and sends nothing;
otherwise the dialog's Submit is clicked and a success marker confirms `applied`. **The Apply click is the
irreversible step on one-click jobs, so a dry run (or auto-submit off) stops before it**: it only screenshots
the job page with the button visible and reports `dry_run` ("stopped before the Apply click").

Selectors (`DEFAULT_SELECTORS` in `apply.py`, `submit` / `success` / `fields` in the YAML) are **fixture-only**:
the logged-in Apply flow (`/profile/all-jobs?jobid=<id>`, the questionnaire markup, success wording) could
not be observed without an account. The public page's own code shows "Apply to this job" / "Apply now"
buttons that, logged in, navigate to `/profile/all-jobs?jobid=<_id>`, and the app bundle names
`UnauthJobApply` / `NotAppliedOnUnauthJob` modals, nothing more.

## Known gaps / for the wiring agent

- Add `"cutshort"` to `pipeline.apply.ASSISTED_SITES`: the generic `FormRunner` must never run on
  `cutshort.io/job/...` (it follows "Apply" buttons, which here is the final action). The YAML `detect` makes
  the profile available for the questionnaire only; it lists no `steps`.
- Register `CutshortCollector` in `collectors/registry.py` with `skip_if=public_cooldown_active` and
  `on_rate_limited=set_public_cooldown` (via `session_scope`), and a `cutshort:` block in `searches.yaml`
  (`enabled`, `location`, `hours_old`, `max_results`, `max_requests`, `remote_only`, `hub_slugs`).
- First logged-in run: confirm the questionnaire selectors, the success marker and whether the Apply click
  can submit without a questionnaire (the runner assumes it can). Record the live markup as fixtures.
- Hub coverage: only the ~21k hub slugs exist; an exotic query falls back to its first token
  (`backend-jobs`) or to nothing. Pass explicit `hub_slugs` for precision.
- Freshness is bounded by the 4 h CDN cache; `hours_old` below 4 still works but may return nothing.
- `apply_url` is the public job page; the logged-in `authApplyUrl` is kept in `raw.auth_apply_url`.
