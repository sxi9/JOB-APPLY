# Workday (`<tenant>.wd<N>.myworkdayjobs.com/<site>`)

Package: `src/jobapply/sites/workday/` (`tenant.py`, `session.py`, `account.py`, `wizard.py`, `apply.py`).
Profile: `config/form_profiles/workday.yaml`. Tests: `tests/test_workday_tenant.py`, `tests/test_workday_account.py`,
`tests/test_workday_wizard.py`, `tests/test_workday_apply.py` (`uv run pytest tests/test_workday*.py -q`). Same shape as
the Naukri package. Wired (docs/STATUS.md section 17, docs/EXTERNAL_APPLY.md): `workday` is in
`pipeline.apply.SITE_APPLIERS` / `ASSISTED_SITES` and `users.ACCOUNT_SITES`, so `jobapply sites apply -s workday`
(`apply_assisted_user`) applies every Workday job under the shared gate stack, wherever the job came from (the
ATS pool, a LinkedIn offsite resolution, a Naukri / Instahyre "apply on company site": those hand the job over
instead of running the FormRunner / navigator); `jobapply sites check -s workday` lists the stored tenant accounts,
`jobapply sites forget-account -t <tenant>` drops one; the Settings site row counts the accounts and points at the
Mailbox; no scheduler task yet (CLI only, like the other assisted sites).

Workday is an *account* site: every tenant wants a candidate account (email + password), confirms the email with a
mailed code or link, and then runs a fixed wizard. This package creates that account with the user's profile email and
a **generated** password kept in the vault, reads the verification mail from the user's own mailbox, and walks the
wizard with the existing `FormRunner`.

## Live observations (2026-10-29, logged out, headless Chromium, 4 page loads, no account created)

`https://revantage.wd1.myworkdayjobs.com/Revantage/job/Bengaluru/Associate--Information-Security-Analyst_JR104387`

| Page | Seen live (`data-automation-id`) |
|---|---|
| Job page | `jobPostingHeader`, `adventureButton` = `<a role=button href=".../apply">Apply</a>`, `utilityButtonSignIn`; the job details render client-side ~5-10 s after `domcontentloaded` (skeleton `skeletonJobDetails` first) |
| Apply dialog (`.../apply`) | `autofillWithResume`, `applyManually`, `useMyLastApplication` (all plain links to `.../apply/<method>`), `closeButton` |
| Apply flow (`.../apply/applyManually`) | `applyFlowPage`, `jobTitleHeading`, `backToJobPosting`, `progressBar` with `progressBarActiveStep` / `progressBarInactiveStep` (li text "current step 1 of 6" + label). Steps on this tenant: **Create Account/Sign In -> My Information -> My Experience -> Application Questions -> Voluntary Disclosures -> Review** |
| Sign in | `signInContent`, social buttons (`GoogleSignInButton`, `LinkedInSignInButton`) and `SignInWithEmailButton` that reveals `form[data-automation-id=signInFormo]`: `formField-email` > `input[data-automation-id=email]` (type text, id `input-6`), `formField-password` > `input[data-automation-id=password]`, `signInSubmitButton` (type submit, inside `noCaptchaWrapper` / `click_filter`), `createAccountLink`, `forgotPasswordLink`, honeypot `input[data-automation-id=beecatcher][name=website]` ("for robots only") |
| Create account | same form plus `verifyPassword`, `passwordRulesList` (alphabetic, numeric, uppercase, special, lowercase, >= 8), one required `createAccountCheckbox` ("I understand and acknowledge the terms of use."), `createAccountSubmitButton`, `signInLink`; the same honeypot |

Not observed (behind the account): what follows *Create Account* (verification mail wording / code input), the wizard
step markup, `Self Identify`, and the Submit button. Those selectors are the standard Workday ids and are
**fixture-only** (inline "mini Workday" page in `tests/test_workday_account.py`).

## Tenant (`tenant.py`)

```python
from jobapply.sites.workday.tenant import parse_tenant_url
t = parse_tenant_url(".../Revantage/job/Bengaluru/Title_JR104387/apply/applyManually")
t.tenant, t.wd, t.site, t.locale, t.job_path   # "revantage", "wd1", "Revantage", None, "/job/Bengaluru/Title_JR104387"
t.apply_url, t.apply_manually_url, t.autofill_url, t.candidate_home_url, t.req_id, t.vault_site  # "workday:revantage"
```

Handles the optional `/en-US/` locale segment, `/job/` and `/details/` paths, the `/apply[/method]` tail and query
strings. The candidate account is per **tenant** (every career site of one tenant shares a Candidate Home), so the vault
key is `workday:<tenant>`; the site is stored inside the secret for reference.

## Account step (`account.py`)

- `classify_page(page) -> login | create | verify | wizard | submitted | challenge | unknown`. Order: CAPTCHA
  (`forms.fields.detect_captcha`) or bot-wall wording -> `challenge`; confirmation / "already applied" -> `submitted`;
  a code input or "verify your email / check your inbox" wording without a password field -> `verify`;
  `verifyPassword` -> `create`; a password field, `signInSubmitButton` or `SignInWithEmailButton` -> `login`; a progress
  bar past step 1 (or the wizard's Next button) -> `wizard`. The challenge wording is deliberately narrow: a job
  description for a security role is on the apply pages too.
- `generate_password()` -> 20 characters satisfying the published rules (never logged, never in a result).
- `WorkdayAccount(page, user, db, tenant, vault=...)`:
  - `credentials()` reads `workday:<tenant>` / `login` from the vault; `new_credentials()` generates and stores one
    **before** the Create Account click (a crash after the click cannot lose it); `mark_verified()` flips `verified`.
  - `open_apply(job_url, method="manual"|"autofill")`: job page -> `adventureButton` -> `applyManually` (or the
    method URL directly, both are links) -> waits for a decisive state. HTTP 403/429 -> `challenge`.
  - `sign_in(email, password)`: clicks `SignInWithEmailButton` when the form is covered, fills `email` / `password`,
    clicks `signInSubmitButton`. The honeypot is never touched. Returns `wizard`, `login` (rejected; the error banner
    is logged), or `verify`.
  - `create_account(email, password)`: `createAccountLink` -> email, password, `verifyPassword`, **only**
    `createAccountCheckbox` (a marketing checkbox stays unchecked) -> `createAccountSubmitButton`.
  - `verify_email(reader, timeout=180)`: `wait_for_verification` polls the IMAP reader (`mail.otp.extract_code` /
    `verification_link` on mails from `myworkday.com` / `workday.com`), types a code into the verification input (or
    opens a link in the same page and returns to the apply flow), and reports `(state, reason)`; nothing arriving within
    the timeout -> `"verification code not received within N s"`, a rejected code -> `"verification code not accepted"`.
    One attempt, never a loop.

## Wizard (`wizard.py`, `config/form_profiles/workday.yaml`)

`WorkdayWizard(runner).run(page, submit=False, screenshot_path=...) -> WizardResult`. For every step read from
`progressBarActiveStep` (aliases: "Voluntary Self-Identification" -> Voluntary Disclosures, ...):

1. The step's own selector map from `workday_steps` in the YAML is installed as the `workday` profile of the
   `FormRunner` (so the profile match rate is judged per step, not against the union), the My Experience *Add* buttons
   are clicked when a block is collapsed, and `FormRunner.run(page, ats_hint="workday", submit=False)` fills it: label
   profile first (Workday inputs have generated ids, every field has a `<label for>` inside
   `div[data-automation-id="formField-*"]`), then the answer bank / heuristics / LLM for the required rest, custom
   dropdowns through the runner's combobox code. `Application Questions` is intentionally unmapped (answer bank first).
2. A screenshot `<shot>-<step>.png` under `data/screenshots/<user_id>/`.
3. Required fields left empty -> stop (`paused`, "<step>: required fields left empty"), a CAPTCHA -> `challenge`.
   Low-confidence answers are filled, flagged (`needs_human`) and the walk continues so every step is reviewable, but
   Submit is then never clicked.
4. *Save and Continue* / *Next* (`bottom-navigation-next-button`) is clicked (not a submit; the dry-run submit guard is
   lifted for that one click and restored). A button whose text says *Submit* is never clicked here. If the progress
   bar does not move and an error banner (`errorMessage` / `[role=alert]`) shows, the run pauses with it.
5. **Review**: screenshot; with `submit=True` and nothing flagged, *Submit* is clicked once and the confirmation
   (`applySuccessPage`, "Thank you for applying" ...) checked; no confirmation -> `paused` "verify on the Candidate Home".

`workday_vault_view(user, app)` adds the `derived.*` keys the YAML uses (address pieces from `profile.location`, work
history and education from the master JSON resume, `skills_text`, `today`).

## Apply (`apply.py`)

```python
from jobapply.sites.workday.apply import apply_job, apply_workday_user, apply_workday, pick_workday_applications
apply_job(session, application, *, dry_run, auto_submit) -> {"status", "reason", "screenshot", "tenant", "account_state",
    "account_created", "steps", "answers", "needs_human", "unfilled_required", "recoveries", "reached_review",
    "submit_clicked", "submitted", "login_status", "stop_run", "apply_url", "ats_type", "url"}
```

`session` is a `WorkdaySession` (persistent headless profile `browser-profiles/<user>/workday`, page, user, db).
Statuses: `applied | dry_run | needs_human | paused | skipped | failed` (`ApplyResult.to_fill_result()` maps them onto
the pipeline's `FillResult` like Naukri does).

| Situation | Result |
|---|---|
| not a Workday URL / page did not load | `failed` |
| CAPTCHA / block (any point) | `paused`, `login_status="challenge"` (orchestrator: 6 h cooldown, run stops) |
| already applied | `skipped` |
| stored credentials -> sign in -> wizard | continues |
| stored credentials rejected | `paused` "workday sign-in rejected on <tenant>: ..." (reset the password on the Candidate Home, then update the vault) |
| no credentials, **global dry run** | `dry_run` "no account on <tenant> yet; it would be created ... (not created)", screenshot. Nothing typed, no password generated |
| no credentials, real run, no vault key | `paused` "vault not configured", `stop_run` |
| no credentials, real run, no mailbox (`mailbox` / `imap` vault entry) | `paused` **"mailbox not configured"**, `stop_run` |
| no credentials, real run, mailbox | password generated -> account created -> code/link read from the mailbox (`MAIL_TIMEOUT_S` = 180 s) -> wizard |
| verification mail missing / code rejected | `paused` "verification code not received ..." / "... not accepted" |
| wizard walked, Review reached, Submit not allowed | `dry_run` "global dry run: Review reached, Submit not clicked" or "auto_submit off for workday: ..." |
| Submit clicked and confirmed | `applied` |

### Safety gates

- **Global dry run** (`JOBAPPLY_DRY_RUN` / admin toggle, both must be off): no account is created, nothing is submitted;
  the wizard is still filled up to Review and screenshotted. Signing in with an existing stored account *is* allowed in
  dry run (it creates nothing).
- **Kill switch**, **site enabled**, **cooldown after a challenge** (`user.preferences["workday_cooldown_until"]`),
  **per-user and per-site daily caps** (`users.SITES["workday"]` = 10/day), **`auto_submit`** (off by default: Submit is
  never clicked) and the **active hours** (`JOBAPPLY_WORKDAY_ACTIVE_HOURS` once settings grow it, else the LinkedIn
  window) sit in `apply_workday_user` / `apply_workday`, mirroring `sites.naukri.apply`.
- CAPTCHA, a missing verification mail, a rejected code or an unexpected page pause with a clear reason; nothing loops.
- The account password is generated per tenant, stored only in the vault (`workday:<tenant>`, kind `login`), never the
  mailbox password, never printed or returned. The `beecatcher` honeypot is never filled.

## Rate limits observed

Four logged-out page loads (job page, apply dialog, apply flow twice) answered 200 with no challenge; no further probing
was done. The orchestrator keeps Naukri's 8-20 s jittered pause between applications and the 6 h cooldown on a challenge.

## Known gaps

- Fixture-only: the verification mail / code input, every wizard step's fields (dates are month/year spinners on real
  tenants and are not filled yet: `work_start_*` extras exist but no selector maps them), the Add buttons of My
  Experience, Self Identify, the Submit button and the confirmation page. The first real run must be a dry run with the
  per-step screenshots reviewed; `selectors=` on `apply_job` / `WorkdayAccount` overrides any id without a code change.
- "Autofill with Resume" is reachable (`open_apply(method="autofill")`) but `apply_job` always applies manually: the
  autofill upload page sits behind sign-in and was not observed. The resume is uploaded on My Experience instead
  (`input[type=file]` -> `derived.resume_path`, the tailored PDF when the application has one).
- Social sign-in (Google / LinkedIn buttons) is never used.
- Wired into `pipeline.apply` (`SITE_APPLIERS` / `ASSISTED_SITES` / `users.ACCOUNT_SITES`), the `jobapply sites`
  CLI and the Settings site card (docs/STATUS.md section 17); `apply_assisted_user(..., "workday")` is the pipeline
  entry point and `apply_workday_user` the package's own equivalent. Still no scheduler task (CLI only), and
  `connect.py` deliberately knows nothing about Workday (no cookie import for an account site).
- `FormRunner._locate_form` follows an "Apply" link when a step shows fewer than three fields; no Workday wizard step
  seen so far has such a link, but a tenant with one would need a local guard in `wizard.py`.
