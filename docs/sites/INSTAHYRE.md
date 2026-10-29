# Instahyre (instahyre.com)

Package: `src/jobapply/sites/instahyre/` (`search.py`, `session.py`, `apply.py`). Tests:
`tests/test_instahyre_search.py`, `tests/test_instahyre_session.py`, `tests/test_instahyre_apply.py`
(`uv run pytest tests/test_instahyre*.py -q`, no network). Research done 2026-10-29, logged out only: no
account or cookies were available, so everything marked *from the app code* was read from the site's
AngularJS bundles, not seen on a signed-in page.

## What is public, what needs a login

| Part | Login? | Verified |
| --- | --- | --- |
| Listing pages `/search-jobs?skills=...`, `/python-jobs`, `/jobs` (301 to `/search-jobs`) | no | live |
| `GET /api/v1/job_search` (the JSON the listing page calls) | no | live |
| Opportunity page `/job-<id>-<slug>/` (server-rendered, JobPosting ld+json) | no | live |
| `GET /api/v1/employer_public_jobs/<id>` (JSON job record) | no | live |
| `/candidate/opportunities/` (the signed-in feed) | yes: `302 /login/?next=...` | live |
| `GET /api/v1/candidate_opportunities...` logged out | answers HTTP 500 | live |
| Apply ("Apply now" on the public page) | yes: the button only redirects to sign-up | live |
| Apply request `POST /api/v1/candidate_opportunities/candidate_matching/apply/` | yes | from the app code |
| Post-apply prompts (active check, go premium, follow us, bulk apply) | yes | from the app code |

## Collector (`InstahyreCollector`, logged out, httpx)

- `InstahyreCollector(queries, location="", hours_old=24, max_results=50, max_requests=40, user_id=None,
  job_type="full_time", years=None, with_details=True, max_details=20, delay=1.5, ...)`,
  `collect() -> list[NormalizedJob]` with `source="instahyre"`, `ats_type="instahyre"`, `url == apply_url` =
  the opportunity page. `user_id` is accepted for interface uniformity and unused: nothing needs a login.
- Search: `GET /api/v1/job_search` with `skills=<query>&company_size=0&job_type=1&isLandingPage=true&search=true
  &limit=20&offset=<n>` (+`years=<max experience>`). 20 cards per page (`LANDING_PAGE_LIMIT` in the app),
  paginated with `offset`, `meta.total_count` / `meta.next` tell when to stop. `skills` takes free text
  (`python developer` gave 9948 results, `python` 4246) and may be repeated; a comma-joined value answers
  `400 {"__all__": ["Invalid format in skills. Use individual items without commas."]}`. `job_type`: 0 both,
  1 full-time, 2 internship. `location=<city>` is **silently ignored** by the server (a Bangalore query returned
  Udaipur and Mumbai rows and `meta.next` dropped the param), so the collector filters by location client-side
  (substring on the card's `locations`; a "Work From Home" posting matches any location).
- Card fields used: `id`, `candidate_title`/`title`, `locations` (comma-joined string, e.g. `Mumbai,Navi
  Mumbai`), `keywords`, `public_url`, `employer.company_name` (+ tagline, employee_count, company_founded, id).
  There is **no posting date** on a card.
- Details (`with_details`): one `GET <public_url>` per job, parsed from the
  `<script type="application/ld+json">` JobPosting block (`title`, HTML `description`, `datePosted` (day
  granularity), `employmentType`, `jobLocation[].address.addressLocality`) plus `<span class="experience">4-8
  Years</span>` and the "Posted by" `span.rec-name`. `hours_old` is applied on the day of `datePosted`; a job
  whose page was not fetched (over `max_details`, budget) keeps `posted_at=None` and is kept.
  `/api/v1/employer_public_jobs/<id>` (`parse_job_json`) is the JSON alternative (`description`,
  `workex_min/max`, `is_active`, `locations` list, `recruiter_name`) but has no date, so the page is preferred.
- Headers: the Chrome UA from `sites.browser.DESKTOP_USER_AGENT` with Chrome client hints; the API call adds
  `Accept: application/json, text/plain, */*`, `X-Requested-With: XMLHttpRequest`, `Referer:
  https://www.instahyre.com/search-jobs` (what the Angular `$resource` sends); the page fetch uses navigation
  headers. Sent per request, so an injected shared client keeps them.
- Blocks: HTTP 403 / 429 / 503, a Cloudflare body ("Just a moment", `challenge-form`, `cf-turnstile`) in a 200,
  or a redirect to `/login/` stop the run, keep what was collected and call `on_rate_limited(hours)` (2 h, or
  `Retry-After` when larger). `skip_if()` lets the registry skip a run while a cooldown is active. Budget:
  `max_requests` across search pages and opportunity pages, with a slice reserved for pages when the budget
  exceeds `max_details`.

## Live observations (2026-10-29, ~14 requests, 2-3 s apart)

- httpx with Chrome-like headers: every public page and both JSON endpoints answered 200 on the first request;
  a `csrftoken` cookie is set; no rate limiting seen at this volume. No 429 observed.
- **Headless Chromium (Playwright, bundled build) got a Cloudflare interstitial on its first visit**:
  `403`, URL rewritten with `__cf_chl_rt_tk=...`, title "Just a moment...". The listing page itself is an
  AngularJS SPA (`ng-app="auctioned"`, `ng-controller="opportunitiesListCtrl"`) so it could not be scraped
  that way anyway; the collector uses httpx, which was not challenged.
- The site is Django + AngularJS 1.2 behind Cloudflare; static bundles on `static.instahyre.com`.

## Session (`session.py`)

- `classify_page(page) -> "ok" | "login" | "challenge" | "unknown"`, without navigating. Order: Cloudflare
  challenge (URL token `__cf_chl`, `#challenge-form`, `.cf-turnstile`, `iframe[src*='challenges.cloudflare.com']`,
  a captcha widget via `forms.fields.detect_captcha`, title "Just a moment" / "Attention Required") >
  login URL (`/login`, `/candidates/register`, `/candidate/register`, `/signup`, `/join`) > signed-in markup
  (`[ng-click*='submitChoice']`, `a[href*='/logout']`, candidate nav links) > logged-out markup
  (`#nav-user-login`, `#nav-user-signup`, `form[ng-submit='loginUser()']`, `[ng-click*='setPostAuthRedirectionUrl']`)
  > a page under `/candidate/` that was not redirected away (ok) > unknown. The logged-out selectors and the
  redirect are live-verified; the signed-in selectors are from the app code and template (the logged-in nav
  markup was never seen) so a signed-in page may classify `unknown` until the apply handler renders.
- `InstahyreSession(context, user, db).check(page)` opens `/candidate/opportunities/`, treats HTTP 403/429/503
  as a challenge, polls until decisive and records `login_status` on the `instahyre` `UserSite` row with the
  dashboard vocabulary (`ok | expired | challenge | unknown`; classify's `login` is stored as `expired`).
  A challenge sets a 6 h cooldown in `user.preferences["instahyre_cooldown_until"]`; `throttled(user, site)`
  reads it. `unknown` never downgrades a stored `ok`.
- `check_session(user_id, engine=None) -> str`: opens the persistent profile **headless**
  (`sites.browser.launch_persistent(pw, user_id, "instahyre", headless=True)`), runs `check`, closes.
- `import_cookies(user_id, path, engine=None) -> str`: `sites.browser.import_cookies_file(user_id, "instahyre",
  path, allowed=("instahyre.com",))` then `check_session`. Raises `ValueError` when the export holds no
  instahyre.com cookies. Cookie values are never logged.

### How to log in (cookie export)

1. Sign in to instahyre.com in a normal browser (handle the reCAPTCHA / any OTP there).
2. Export the site's cookies with a cookie-export extension as JSON (the common
   `[{name, value, domain, path, expirationDate, secure, httpOnly, sameSite}]` shape, or `{cookies: [...]}`).
3. `import_cookies(user_id, "<path>.json")` loads them into `data/browser-profiles/<user_id>/instahyre` and
   returns the session status. The session cookie is `sessionid` (Django); `csrftoken` comes along.
4. Cloudflare's `cf_clearance` cookie is bound to the browser's UA and IP, so a clearance obtained in Chrome
   does not transfer to the headless profile: the first headless visit may still land on the interstitial
   (`challenge`, cooldown, `login_status=challenge`). This is the main open risk, see below.

## Apply (`apply.py`)

- `apply_job(session, application, *, dry_run, auto_submit, page=None, selectors=None, screenshot_path=None,
  pause=None) -> {"status", "screenshot", "reason", "needs_human", "interest_sent", "result"}` where
  `session` is an `InstahyreSession`, `status` is `dry_run | applied | paused | failed | skipped` and `result`
  is a `forms.runner.FillResult` (`profile_origin="builtin:instahyre"`) ready for `pipeline.apply.record_outcome`.
- Gates inside `apply_job` (before the browser is touched): kill switch -> `skipped`; site row missing /
  disabled -> `skipped`; cooldown -> `skipped`; `dry_run` OR global dry-run switch -> never clicks;
  `auto_submit` AND the site row's `auto_submit` -> otherwise stops before the click with a screenshot
  (`status=dry_run`, reason says why); per-user `daily_cap` and the site's `daily_cap` (default 15) via
  `pipeline.apply.applied_today` -> `skipped`; real submits only inside the active hours
  (`JOBAPPLY_INSTAHYRE_ACTIVE_HOURS` once settings grow it, else `linkedin_active_hours`).
- Flow on the page: `goto` (403/429/503 -> `paused`, cooldown) > classify (challenge -> `paused` + screenshot
  + cooldown + `login_status=challenge`; login page or the sign-up bound button -> `paused`
  "instahyre login needed", `login_status=expired`) > already-applied marker -> `failed` (never retried) >
  Apply control (`button#job_details_top`, `#apply-bar button`, `[ng-click*='submitChoice...']`,
  `button.btn-interested`, `button:has-text('Apply' | 'Interested')`) missing -> `failed` > a modal with form
  controls already open -> `paused` "screening questionnaire needs a human" (nothing is filled) > dry run /
  auto-submit off: screenshot and stop > click, then poll up to 12 s for: a growl error (`.growl-item.alert-danger`
  -> `failed` with its text), the success toast (`#toast-container .toast-success`, "Application sent to
  X!") or an applied marker, a modal with form controls, a challenge. Success toast + questionnaire modal ->
  `paused` "post-apply questionnaire needs a human" with `interest_sent=True` (the application IS registered on
  the site; `FillResult.submitted` stays False so it books as paused, the wiring agent can count
  `interest_sent` toward the cap as `apply_instahyre_user` does). Success toast + a modal without controls
  (premium / follow / bulk-apply prompt) -> closed, `applied`. No toast at all -> `paused` "apply clicked but
  no confirmation seen" (never retried automatically).
- `pick_instahyre_applications`, `apply_instahyre_user(pw, session, user, limit, dry_run)` and
  `apply_instahyre(username, limit, dry_run, engine)` mirror the LinkedIn Easy Apply runner (headless
  persistent profile, session check first, 8-20 s between applications, stop the user's run on a challenge
  or a logout, notify through `jobapply.notify`).
- Selectors live in `apply.DEFAULT_SELECTORS` and can be overridden per call (`selectors=`); `config/sites/`
  is outside this package's remit.

### Why there is no `config/form_profiles/instahyre.yaml`

The apply is one click with no form, and the questionnaire is handed to a human, so there is nothing for a
form profile to map. A YAML there would also be loaded as a builtin ATS profile (`forms.profiles
.load_builtin_profiles` reads every file in the folder, `FormRunner` would pick it through `ats_hint=
"instahyre"`) and `tests/test_forms.py` pins the builtin set to the five ATSes.

## Rate-limit behaviour observed

None at ~14 requests with 2-3 s delays. Cloudflare challenged headless Chromium immediately but never the
httpx client. The collector still treats 403/429/503 and any Cloudflare body as a block with a cooldown.

## Known gaps

- Signed-in markup (nav, the exact Apply / Interested button, the questionnaire modal, the post-apply prompts)
  was reconstructed from the app code and the templates in the public pages, not observed. The tests run the
  real runner in headless Chromium against synthetic pages built from that reconstruction. First real run must
  be a dry run with a screenshot review; selectors can be overridden via `selectors=`.
- Headless Chromium vs Cloudflare: the logged-in profile may be challenged on its first visit even with valid
  session cookies (`cf_clearance` is not transferable). The run then pauses with `login_status=challenge` and
  a 6 h cooldown; a headed browser is not available on this machine to clear it. If this bites, the apply path
  needs either a headed session once per profile elsewhere, or the API call
  (`POST /api/v1/candidate_opportunities/candidate_matching/apply/`, JSON `{"job_id": <id>, "is_interested":
  true, "is_non_matching_application": true}` with the `X-CSRFToken` header from the `csrftoken` cookie) sent
  through httpx with the imported cookies. That endpoint was not exercised (no account).
- `location` is client-side only; the server-side form of the filter (if any) is unknown.
- No posting date on search cards: `hours_old` only applies to jobs whose page was fetched.
- Experience filtering: `years` is the API's max-experience filter and is honoured; `workex_min/max` from the
  page land in `raw` for the scorer.
- The logged-out `employer_public_jobs` endpoint and the page both expose the recruiter's name; it is stored in
  `raw["posted_by"]` only.
