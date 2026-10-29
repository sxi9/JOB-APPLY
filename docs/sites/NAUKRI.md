# Naukri (naukri.com)

Package: `src/jobapply/sites/naukri/` (`search.py`, `session.py`, `apply.py`, `questions.yaml`).
Tests: `tests/test_naukri_search.py`, `tests/test_naukri_session.py`, `tests/test_naukri_apply.py`
(`uv run pytest tests/test_naukri*.py -q`). Same shape as the LinkedIn package; nothing here is wired into the
registry, CLI, scheduler or web yet.

## Public vs login-only

| Part | Login? | How it is used |
|---|---|---|
| Job search (`/jobapi/v3/search`, the `<keyword>-jobs` SRP page) | no | `NaukriCollector` (API first, headless browser fallback) |
| Job page (`/job-listings-...-<jobId>`) read-only | no | the `Apply` control reads "Login to apply" when logged out |
| **Apply** (one click) and the post-apply chatbot | yes | `apply_job` / `NaukriApplyRunner` in the user's persistent profile |
| **Apply on company site** | yes to read the URL from the live page | `apply_job` returns `status="external"` + `apply_url` / `ats_type` |
| Profile page (`/mnjuser/profile`) | yes | `refresh_profile` |
| Homepage (`/mnjuser/homepage`) | yes | `check_session` / `NaukriSession.check` decide `ok / login / challenge / unknown` |

## Live observations (2026-10-29)

Logged-out, plain `httpx` from this machine, polite and single-shot:

- `GET https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword&searchType=adv&keyword=python+developer&pageNo=1&jobAge=1&seoKey=python-developer-jobs&src=jobsearchDesk&latLong=`
  with `appid: 109`, `systemid: Naukri`, `gid: LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE`, a desktop Chrome UA and a
  naukri.com Referer answered **`HTTP 406`**, `content-type: application/json`, body
  `{"message":"recaptcha required","statusCode":406,"validationErrors":[],"data":null}` and set two cookies
  (`_t_ds`, `J`). No `Retry-After`, no rate-limit headers. The probe stopped there (first 4xx).
- A headless-browser probe of the public SRP was **not** run (the harness refused a browser launch with
  anti-automation flags), so the SRP card markup, the shape of the API JSON and every selector below are
  **fixture-only**, written from the 2025-2026 Naukri React markup and verified only on inline test fixtures.

So: the API is reachable from an IP that Naukri trusts (it is what the public SRP calls), but from here it
wanted a reCAPTCHA on the very first request. The collector treats 406 / 403 / 429 / non-JSON as a block, never
retries, and (when a user profile is available) falls back to the SRP page inside that profile, where the page's
own XHR to the same API is captured.

## Collector

```python
from jobapply.sites.naukri.search import NaukriCollector
NaukriCollector(queries, location="", hours_old=24, max_results=50, max_requests=40, user_id=None).collect()
```

- `name = source = ats_type = "naukri"`, `key = "naukri:search"`. Returns `NormalizedJob` rows (the same type every
  collector returns): `title`, `company`, `location` (placeholder `location`), `posted_at` (aware UTC: `createdDate`
  epoch ms, else `footerPlaceholderLabel` "Just now" / "3 Days Ago" / "30+ Days Ago"), `url = apply_url` = absolute
  job page from `jdURL`, `description` = `jobDescription` text plus `Skills: ...` from `tagsAndSkills`,
  `salary_min/max/currency` from the salary placeholder ("5-10 Lacs PA" -> 500000-1000000 INR; "Not disclosed" ->
  None), `raw` = `skills`, `experience`, `salary`, `posted_label`, `company_id`, `job_type`, `vacancy`, `rating`.
- API params: `noOfResults=20`, `urlType=search_by_keyword` (or `search_by_key_loc` with `location`),
  `searchType=adv`, `keyword`, `location`, `pageNo`, `jobAge=<days>` (`ceil(hours_old/24)`), `seoKey=<slug>-jobs[-in-<loc>]`,
  `src=jobsearchDesk`. Headers are sent on **every** request (an injected client gets them too).
- Pagination by `pageNo` until `noOfJobs`, `max_results` or `max_requests` (search pages and browser loads share the
  budget). Jobs older than `hours_old + 12 h` are dropped (jobAge is whole days; undated jobs are kept).
- Blocks: `rate_limited=True`, `block_reason` ("recaptcha required", "http 403", ...), the run keeps what it has and
  the `on_rate_limited(hours)` hook fires once at the end of `collect()` (default 2 h, `Retry-After` honoured). It
  does not fire when the browser fallback got through. `skip_if()` skips the run during a cooldown (wire it to an
  `app_settings` key the way `linkedin_guest_cooldown_until` is wired).
- Browser fallback (`user_id` set, or a `page_factory` in tests): `launch_persistent(pw, user_id, "naukri",
  headless=True)`, `GET https://www.naukri.com/<slug>-jobs?k=...&l=...&jobAge=N`, prefer the captured
  `/jobapi/v3/search` JSON, else parse `div.srp-jobtuple-wrapper[data-job-id]` cards (`a.title[href]`, `a.comp-name`,
  `span.expwdth`, `span.sal span`, `span.locWdth`, `span.job-desc`, `ul.tags-gt li.tag-li`, `span.job-post-day`).
  A CAPTCHA page (`iframe[src*=captcha]`, `.g-recaptcha`) stops it. Search needs no login; with no `user_id` and a
  blocked API the collector logs `no user profile for the browser fallback` and returns what it has. The shared
  `assisted.naukri` block in `config/searches.yaml` does not accept `user_id` on purpose: the pool search for every
  user must not run inside (and risk a CAPTCHA on) one user's logged-in profile; the fallback is for per-user calls.

## Session

```python
from jobapply.sites.naukri.session import classify_page, check_session, import_cookies, NaukriSession, throttled
```

- `classify_page(page) -> "ok" | "login" | "challenge" | "unknown"`. Order: challenge URL (`/captcha`, `/otp`, ...)
  or CAPTCHA selector -> `challenge`; `/nlogin/login` URL -> `login`; logged-in header
  (`div.nI-gNb-drawer__icon`, `img.nI-gNb-icon-img`, `a[href*='/mnjuser/profile']`, ...) -> `ok`; login form /
  header links (`#usernameField`, `#passwordField`, `a#login_Layer`, `a#register_Layer`) -> `login`; on a non-content
  page, text such as "recaptcha required", "access denied", "enter otp", "unusual activity" -> `challenge`. Job pages
  and `/mnjuser/` pages are content pages: their text is never scanned, so a JD mentioning "security check" is safe.
- `check_session(user_id, session_db=None)` opens the persistent profile **headless**, loads
  `https://www.naukri.com/mnjuser/homepage`, waits for a decisive selector and records the result on the `naukri`
  `UserSite` row (`login_status`, `last_login_at`). `unknown` never downgrades a stored `ok`. HTTP 403/406/429 on
  the homepage counts as `challenge`.
- `import_cookies(user_id, path, session_db=None)` -> `check_session` result. Uses the shared
  `sites.browser.import_cookies_file` with `allowed=("naukri.com",)`; cookie values are never logged or returned.
- `NaukriSession(context, user, db, page=None)`: `.page` (lazy), `.check()`, `.record_challenge()` (status
  `challenge` + 6 h cooldown in `user.preferences["naukri_cooldown_until"]`), `.record_login_required()`;
  `throttled(user, site)` is True during a cooldown or while `login_status == "challenge"`.

### How to log in (cookie export)

1. Log in to naukri.com in your own browser (email/OTP as usual).
2. Export the cookies for `naukri.com` as JSON with a cookie-editor extension (the common
   `[{name, value, domain, path, expirationDate, secure, httpOnly, sameSite}]` shape, or `{"cookies": [...]}`).
3. Save the file somewhere private (it is a login token) and run
   `import_cookies(user_id, "path/to/naukri-cookies.json")`; it answers `ok` when the profile is logged in.
4. Delete the export. Re-run step 1-3 when `login_status` turns `login` (Naukri sessions last weeks, not forever).

There is no `login_interactive`: this machine cannot open a headed window, and the program never types credentials.

## Apply

```python
from jobapply.sites.naukri.apply import apply_job, refresh_profile, apply_naukri_user, apply_naukri
apply_job(session, application, *, dry_run, auto_submit) -> {"status", "screenshot", "reason", "apply_url", "ats_type",
                                                             "answers", "questions", "needs_human", "clicked_apply",
                                                             "submitted", "login_status", "url"}
```

`session` is a `NaukriSession` (page + user + db). Flow on the job page:

1. `goto` -> HTTP 403/406/429 -> `paused`, `login_status="challenge"`.
2. CAPTCHA selector / challenge page -> `paused` + `challenge`, screenshot. Never retried.
3. Wait for the primary control (`button#apply-button`, `button#company-site-button`, `#already-applied`,
   `a#login_Layer`). "Login to apply" / logged-out header -> `paused`, `login_status="login"`.
4. `#already-applied` / "Applied" -> `skipped`. No control -> `failed`.
5. `Apply on company site` -> `external`: `apply_url` from `href` / `data-url`; when the URL is only revealed by a
   click, it is clicked **only** when `dry_run=False and auto_submit=True` (the popup URL is read and the tab closed;
   Naukri may log that click as an apply). `ats_type` from `collectors.detector.detect_from_url`.
6. `Apply`: this click **is** the submit, so it happens only when `dry_run=False and auto_submit=True`; otherwise
   `dry_run` with a screenshot under `data/screenshots/<user_id>/`.
7. After the click: chatbot drawer (`div.chatbot_Drawer`) -> one question at a time: last `.botMsg` text, radio /
   checkbox labels (`.ssrc__radio-btn-container input + label`), or the free-text box
   (`div.textArea[contenteditable]`), then `div.sendMsg`. Answers come from the vault (`questions.yaml` map, the shared
   LinkedIn label rules, `skills` years), then the answer source (`forms.runner.AnswerSource`: answer bank,
   heuristics, LLM via `jobapply.ai.service`) when its confidence is >= 60. Anything else -> `needs_human` with the
   question in `needs_human` and a screenshot; the loop stops at once (the Apply click is already registered on
   Naukri, the reason says so). Confirmation = `[class*='apply-message']`, "successfully applied" / "application
   sent" text, or the button turning into `#already-applied` -> `applied`. A click with no confirmation -> `paused`
   ("verify on Naukri").

`ApplyResult.to_fill_result()` maps the vocabulary onto the pipeline's `FillResult`
(`applied` -> submitted; `dry_run` -> plain; `needs_human` / `paused` / `skipped` / `external` -> `paused_reason`;
`failed` -> `error`) so `pipeline.apply.record_outcome` records it unchanged.

### Orchestrator (`apply_naukri_user`, `apply_naukri`)

Gates in order: kill switch; global dry-run switch (forces `dry_run=True`); `UserSite.enabled`; cooldown
(`throttled`); `NaukriSession.check()` (`login` / `challenge` -> alert and stop, `unknown` -> retry later);
per-user `daily_cap` and the site's `daily_cap` (25 by default); the site's `auto_submit` flag (off -> no click,
screenshot only); 8-20 s human pause between applications; a `challenge` or `login` result mid-run records it and
stops. `apply_naukri` adds the active-hours window (`JOBAPPLY_NAUKRI_ACTIVE_HOURS` once settings grow it, until then
the LinkedIn window). An `external` result writes `job.apply_url` / `job.ats_type` back: a known ATS leaves the
application `approved`/`queued` for the ATS filler (the resolved site must be enabled for that user); an unknown
host, or a URL that needs a click, pauses it for a human.

### `refresh_profile(user_id, dry_run=True, session_db=None, *, page=None, resume_path=None)`

Opens `/mnjuser/profile` headless. `dry_run` -> navigate + screenshot only (`status="dry_run"`). Otherwise clicks
the headline `edit` icon (`#lazyResumeHead .edit`, `span.edit.icon`), re-fills `textarea#resumeHeadlineTxt` with
its own text and clicks Save (`button.btn-dark-ot` / `form button[type=submit]`); with `resume_path` it also
re-uploads the resume through `input#attachCV`. `status="refreshed"` when the editor closed or a `.toast-msg`
appeared; `paused` with `login_status` when logged out / challenged. It mutates the account, so it honours the
switches like an apply run: `skipped` (`kill switch on`) while the kill switch is on, and the global dry-run switch
downgrades `--refresh` to a dry run.

## Rate limits observed

One plain-HTTP request from this IP: 406 recaptcha wall, no `Retry-After`. Defaults chosen accordingly: 1.5 s
jittered delay between requests, `max_requests=40` per run, 2 h cooldown on a block (doubling is the registry's job),
6 h cooldown and a `challenge` login status on a CAPTCHA/OTP seen in the browser profile, never a retry on either.

## Known gaps

- Everything DOM-related (API JSON shape, SRP cards, job-page ids, chatbot drawer, profile editor) is fixture-only;
  the first logged-in run must be a dry run with the screenshots checked, and `questions.yaml` `selectors:` is where
  a drifted selector gets fixed without a code change.
- No `config/form_profiles/naukri.yaml`: Naukri's apply is not a form, and `tests/test_forms.py` pins the exact set of
  built-in profiles (adding one there breaks it). The question map lives in `sites/naukri/questions.yaml` instead.
- The chatbot's multi-select questions are answered with one option only; date pickers and file prompts are not
  handled (they end in `needs_human`).
- `showMultipleApply` (one job, several locations) is recorded in `raw` but not acted on.
- Not wired: registry (`searches.yaml` block, cooldown key), CLI (`naukri search / check / import-cookies / apply /
  refresh`), scheduler task, `ASSISTED_SITES`, docs/SOURCES.md.
