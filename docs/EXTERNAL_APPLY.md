# Applying anywhere: the universal external path

"If anything leads to an external link, it should be able to do it." This page is the map of what happens when a
job's apply link points somewhere other than a known ATS board, how the AI fallback fits in, where the Gmail
mailbox is used for one-time codes, and which gates stand in front of every click that could submit, create an
account or send mail.

```
apply_url
  |
  |-- mailto:                           -> apply by email (email site)            pipeline/apply.apply_by_email
  |-- known ATS (greenhouse, lever,     -> builtin profile + FormRunner           forms/runner.py
  |   ashby, workable, smartrecruiters)
  |-- Workday tenant                    -> sites/workday (account per company)    jobapply sites apply -s workday
  |-- LinkedIn / Naukri / Instahyre /   -> that site's own filler in the user's   jobapply linkedin apply,
  |   Cutshort / Wellfound                 cookie-connected profile                jobapply sites apply -s <site>
  |-- Naukri / Instahyre says           -> resolve the URL, detect the ATS, then one of the rows above
  |   "apply on company site"             (route_external_result)
  |-- LinkedIn posting that is not      -> open it logged in, resolve the real link, detect the ATS, then one
  |   Easy Apply                           of the rows above (apply_external_linkedin)
  `-- anything else                     -> the navigator under the `external` site  forms/navigator.py
```

`collectors.detector.detect_from_url` decides the row. It runs on every resolved URL, so a LinkedIn posting that
resolves to `acme.wd5.myworkdayjobs.com` lands on the Workday row, not on the generic one.

## 1. Unknown host: the navigator

`FormRunner.run` is called with the URL. When the landing page has no usable form (fewer than three visible
fields), `forms/navigator.Navigator.find_form` takes over, at most 6 hops / 120 s:

1. **Cookie / consent banners** are dismissed, least permissive button first (reject, necessary only, then accept).
2. **A form inside an iframe** is opened directly, so filling, screenshots and submit work on a plain page.
3. **Heuristics** score every visible link and button: *Apply*, *Apply now*, *Start application* (100), anything
   containing *apply* (80), *Next* / *Continue* (60); share / save / print / navigation and anything that looks
   final (*Submit*, *Send*, *Finish*, *Confirm*) score 0 and are never followed. The best candidate is followed in
   the same tab, a popup is taken over (its URL opened here, the popup closed), redirects are followed.
4. **AI fallback.** When the heuristics find nothing, the LLM (`ai.service.structured`, task `navigate`) picks
   from a NUMBERED list of the visible controls; the answer must be an index into that list, never free text,
   and final-looking controls are not in the list. With no LLM, with the FakeLLM, or on any model error, only
   the heuristics run; the page is then paused with "no apply control found".
5. **Captcha** on any hop pauses with `captcha on page (...)`.
6. **Account walls** (a visible password field on a short form): see section 2.
7. **mailto:** apply links are reported, not followed; the pipeline applies by email instead.

Once a form is reached the normal filler runs: builtin profile by URL, learned profile by fingerprint, else the AI
field mapper (never sees candidate data; the mapping is saved as a learned profile shared by all users); custom
questions come from the answer bank, then the heuristics, then the LLM (low confidence -> `needs_human`);
inline validation errors get one AI-guided fix (`ai.service.recover_field`). Multi-step wizards are walked with
*Next* / *Continue*, never a final control. Every AI call has a non-AI fallback and the whole path runs with
`llm=None`.

The job keeps `ats_type=None` (no fake ATS is written); the run is governed by the user's `external` site row
(`users.SITES["external"]`: daily cap 10, auto-submit off, lazily created for older accounts).

## 2. Sign-up and the Gmail mailbox (OTP)

An account wall on an unknown host:

- **Stored account** for the host (vault site `ext:<host>`, kind `login`): sign in, also in a dry run (the dry-run
  submit guard is lifted for that one form only).
- **No account yet.** Only when the run may submit (global dry run off AND `external` auto-submit on), AND the
  user's Mailbox is configured: a password is generated (never the mailbox password), stored in the vault
  *before* the sign-up form is sent, the form is filled with the profile email, and the verification is read
  from the mailbox (`mail.otp.wait_for_code` / `wait_for_link`, bounded timeout, one attempt). Then the account
  is marked verified in the vault.
- Every other case pauses with the exact reason: `<host> needs an account; not created (dry run or auto-submit
  off)`, `... mailbox not configured for the email verification`, `account created on <host> but no verification
  mail within N s`, and so on. Nothing loops.

Workday is the same idea with a dedicated package: one account per **tenant** (`workday:<tenant>`, kind `login`),
created with the profile email and a rules-compliant generated password, confirmed from the mailbox, then the
fixed wizard (My Information -> My Experience -> Application Questions -> Voluntary Disclosures -> Review) is
walked by the FormRunner with the per-step selector map from `config/form_profiles/workday.yaml`. See
[sites/WORKDAY.md](sites/WORKDAY.md). The generic navigator refuses to sign up on a Workday host
(`forms.navigator.account_site_for_url`), and the pipeline never sends a Workday job to it: `run_one`,
`_run_resolved_external` and `route_external_result` hand such a job to the Workday applier
(`pipeline.apply.hand_to_account_site`), where `apply_assisted_user(..., "workday")` applies it under the
shared gate stack.

The mailbox itself: Settings -> Mailbox (or the onboarding step) with a Gmail **app password**; the test logs in
over IMAP and SMTP without sending anything. `jobapply mail test -u <user>` and
`jobapply mail wait-code -u <user> --sender workday` from the CLI. [MAILBOX.md](MAILBOX.md) has the walkthrough.
A paused application whose reason starts with "mailbox not configured" links to Settings -> Mailbox from the
Queue page.

## 3. Apply by email

A `mailto:` apply link (HN "Who is hiring", small companies, a mailto found by the navigator) is applied to by
email: the tailored resume attached, `ai.service.application_message` (template without an LLM) as the body.
Dry run or `email` auto-submit off -> a full `.eml` draft under `data/screenshots/<user_id>/` and the
application is paused for review; no SMTP mailbox -> paused "mailbox not configured"; otherwise the mail is sent
and the application is `applied`.

## 4. The gates (they hold on every row of the map)

| Gate | Where |
|---|---|
| Global dry run (`settings.dry_run` / Admin switch): never submit, never create an account, never send; navigate + fill + screenshot only | every entry point ORs it in (`apply_user`, `apply_external_linkedin_user`, `apply_assisted_user`, `apply_by_email`); the navigator's `allow_final` and the FormRunner's `submit` are derived from it |
| Kill switch | same entry points, checked first |
| Per-site auto-submit (off by default) | `UserSite.auto_submit` of the governing site: the ATS row, `external`, `email`, `workday`, or the assisted site |
| Per-user and per-site daily caps | `applied_today` before each application; at the cap the run fills for review only |
| Active hours | `JOBAPPLY_<SITE>_ACTIVE_HOURS` (`workday_active_hours` included), falling back to the LinkedIn window |
| Captcha / OTP not received / challenge | paused with the reason; a challenge records the site's cooldown and ends the run; nothing is retried in a loop |
| Account creation | profile email + a generated per-site/per-tenant password stored in the vault (`ext:<host>` or `workday:<tenant>`, kind `login`) before the click; never the mailbox password; never printed |

## 5. Where to look

- Settings -> Sites: the `external`, `email` and `workday` rows (enable, auto-submit, cap). The Workday row shows
  "account per company, created automatically; needs Mailbox" and how many companies have a stored account;
  there is no cookie upload for it. `jobapply sites check -s workday -u <user>` lists the tenants (never a
  password); `jobapply sites forget-account -u <user> -t <tenant>` drops a stored one.
- Queue: every pause carries its reason (navigator hop list in the detail, screenshot where one was taken).
- Code: `forms/navigator.py`, `forms/runner.py` (`_locate_form`, `_wizard`), `pipeline/apply.py`
  (`route_external_result`, `_run_resolved_external`, `apply_by_email`, `hand_to_account_site`,
  `apply_assisted_user`), `sites/workday/*`, `mail/otp.py`, `mail/mailbox.py`.
- Tests: `tests/test_navigator.py`, `tests/test_apply_routing_external.py`, `tests/test_wiring_workday_external.py`,
  `tests/test_workday_*.py`, `tests/test_otp.py`, `tests/test_mailbox.py`.

## Not verified live

The navigator was smoke-tested read-only on two public boards and one Lever posting (banner dismissed, Apply
followed, hCaptcha detected and paused). The LLM pick, the sign-up / OTP flow, the SMTP send, the Workday
account creation and wizard steps are fixture-only: the first real run on each must be a dry run with the
screenshots reviewed on the Queue page before auto-submit is switched on for that site.
