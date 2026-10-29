# Dashboard

FastAPI backend, HTMX (or small React) front, dark theme, mobile friendly so queued jobs can be approved from a phone. Served behind Tailscale.

## Login
Username and password. Roles: admin (sees all users) and user (own data only).

## Home / Overview
- Stat tiles: new jobs today, queued, applied today vs cap, replies, interviews.
- Funnel: found -> matched -> applied -> replied -> interview -> offer.
- Activity feed with screenshot thumbnails.
- Collector health: each source, last run, count, red when failing.

## Coverage
- Dedicated page (`/coverage`, `web/routes/coverage.py` + `coverage.html`, linked in the nav), per-user scoped through `ctx.view` so an admin "View as" sees that user's match count against the shared pool. Read-only (GET), so no CSRF.
- **Stat tiles:** jobs in pool (total), new in 24h, new in 7d, and how many match the viewed user's filters (= that user's `Application` rows, which exist from the moment a job passes their filters). Tiles link into the relevant `/jobs` views.
- **Matched-vs-total line:** "N match your filters of M total", with a hint that a small matched number means narrow filters, not an empty pool — this answers the recurring "why so few jobs?" confusion on-screen. Points at `/settings` to widen.
- **By sector:** counts pool jobs whose title matches each of the 15 taxonomy fields via `roles.taxonomy.match_title` (Software Engineering, Cybersecurity, Data & AI, ...), shown as labelled bars, biggest first; all fields are listed so the full taxonomy is visible.
- **By source:** jobs grouped by `Job.source` (greenhouse / lever / linkedin / ...) as a small bar table.
- **Pool size** and the **by-source** breakdown prefer the collector side's shared `coverage_stats(session)` helper when it is importable (lazy import, tried on `pipeline.collect` then `pipeline.coverage`); any import error or unexpected return falls back to an inline count, so the page and its tests never depend on that helper having landed. Empty-safe (zero pool renders, no divide-by-zero).

## Jobs
- Filters: posted in last N hours, source, score, location, remote, salary, company.
- Row: title, company, location, posted time, source badge, AI match score, status chip.
- Detail: description, AI reason and missing skills, chosen resume variant, buttons Apply now / Queue / Skip / Blocklist company.

## Queue / Review
- Jobs waiting for approval (auto-submit off, or low confidence).
- Filled answers next to form fields before submit, editable cover letter. Approve / Edit / Reject.
- A paused application whose reason starts with "mailbox not configured" (Workday account creation, apply by email) shows a "Set up your Mailbox in Settings" link to `/settings#mailbox`.

## Applications
- Status timeline, submission screenshot, answers sent, follow-up date, reply thread.

## Inbox
- Mails classified: interview, assessment, rejection, recruiter question, spam.
- Forward to real email or WhatsApp. AI-drafted reply.

## Profile
- Resume uploads and variants, master answers vault, canned answers, skills with years.

## Settings
- Filters: keywords include/exclude, roles, locations, salary floor, seniority.
- Sites: one row per site from `users.SITES` (ATS boards, Workday, the assisted sites LinkedIn / Naukri / Instahyre / Cutshort / Wellfound, email): enable per site, auto-submit on/off (off by default everywhere; the global dry-run switch on Admin still wins), daily cap (`users.py` defaults: naukri 25, instahyre / cutshort / wellfound 15), login status chip (`ok` / `expired` / `challenge` / `unknown`, `n/a` for ATS boards).
  - **Connect** (assisted sites only): upload the site's cookie export (`POST /settings/sites/{site}/cookies`, multipart, CSRF). The file is written to `data/cookies/<user id>/<site>.json` (git-ignored, per user) and imported into that user's own headless browser profile through `sites.connect.connect_site` → `sites.browser.import_cookies_file` (only the site's domains are kept, values never logged). Naukri, Instahyre, Cutshort and Wellfound are verified right away by the site's `check_session` and the Login column shows the result (`login` is stored as `expired`); LinkedIn only imports (same as the onboarding step). A bad export marks the row `expired` and flashes the reason.
  - **Check** (assisted sites): `POST /settings/sites/{site}/check` opens the site headless with the saved profile and refreshes the chip.
  - **Workday** (kind `account`, `users.ACCOUNT_SITES`): no cookie upload and no Re-login button. The row says "account per company, created automatically; needs Mailbox", counts the companies with a stored account (vault rows `workday:<tenant>`, `sites.workday.account.count_accounts`) and shows the mailbox status chip, or a link to set the Mailbox up first. Applying runs through `jobapply sites apply -s workday`; see docs/EXTERNAL_APPLY.md.
  - Applying on these sites runs from the CLI (`jobapply sites apply -s <site> [-u user] [--dry-run|--submit]`, `jobapply linkedin apply`) through `pipeline.apply.apply_assisted`, which honours the kill switch, the global dry-run switch, the site's enabled / auto-submit flags, the user and site daily caps, the site's active hours (`JOBAPPLY_<SITE>_ACTIVE_HOURS`, falling back to the LinkedIn window) and challenge cooldowns; outcomes land on the Queue / Applications pages like any other filler.
- **Mailbox** (`#mailbox`, above Credentials; `web/routes/settings.py` + `mail/mailbox.py`): provider preset (Gmail `imap.gmail.com:993` / `smtp.gmail.com:587`, Outlook, Yahoo, custom hosts under *Advanced*), address and app password (masked, never echoed; blank keeps the saved one). `POST /settings/mailbox` saves vault site `mailbox` kinds `imap` **and** `smtp`; `POST /settings/mailbox/test` (or the "Save and test" button) logs in over IMAP, reads only the newest message's subject (BODY.PEEK, nothing marked), logs in over SMTP without sending, and stores `last_test_at` / `status` / `error` / `newest_subject` in `users.profile["mailbox"]`; the status chip shows `ok` / `untested` / `imap_failed` / `smtp_failed` / `missing`. `POST /settings/mailbox/delete` removes both rows. Links to `docs/MAILBOX.md` (Gmail 2-Step Verification → App passwords). CLI: `jobapply mail test -u <user>`, `jobapply mail wait-code -u <user> --sender workday --timeout 120`.
- Credentials: site logins and mailbox IMAP/SMTP, encrypted at rest, masked in UI.
- AI: API key, scoring threshold, cover letter tone.
- Notifications: WhatsApp number, alert types.

## Admin
- Users, add user, per-user usage and caps, collector schedule, logs, global kill switch.

## Referrals
- Review page for referral drafts (`/referrals`, `web/routes/referrals.py` + `referrals.html`), per-user scoped through `ctx.scope` (admin "View as" respected), CSRF on every POST.
- Lists this user's drafts newest-first, grouped by status (draft / approved / sent / skipped / failed). Each card shows the contact name + headline (name linked to their LinkedIn profile), the related job title + company (linked), the channel, and an editable message body.
- Actions: `POST /referrals/{id}/save` (edit body only), `/approve` (draft → approved), `/unapprove` (approved → draft), `/skip` (→ skipped). Approve/skip are no-ops outside their valid source status.
- **Safety.** Drafts are safe: generating/queuing a draft never contacts anyone, and **approving does not send** — it only marks a draft ready. Sending a LinkedIn connection request or message is a separate, manual, capped step that is **off by default**, gated behind the global referral-send switch, a per-user daily cap, the kill switch, active hours and per-draft approval (the core path `sites.linkedin.referral.send_referral`, in the worker/CLI, not this dashboard). The page shows a banner and an "About LinkedIn automation risk" explainer; it deliberately has no send button, so nothing here can bypass those gates.
- The `ReferralDraft` model is imported lazily in the handlers: the page is empty-safe and still renders if the model is absent.

## Sign-up and onboarding
- **Self-service sign-up** (`/signup`, `web/routes/auth.py` + `signup.html`): anyone can create an account with a unique username + password (PBKDF2 via `users.create_user`) and an optional email. Validation covers username shape (3-32 chars), password length (>= 8) and confirmation, and duplicate usernames; errors come back as flashes with the fields preserved. The new account is always role `user` (admin is never selectable here). On success the user is logged in with the same session mechanism as `/login` and sent to the wizard. Gated by `config.allow_signup` (default True): when False, `/signup` 404s and the login page hides the "Create account" link. The first user is still bootstrapped by the CLI (`jobapply create-user`).
- **Mailbox step** (`?step=mailbox`, between answers and the connect step): the same form and handlers as Settings → Mailbox (`settings.save_mailbox_form` / `settings.run_mailbox_test`), posting to `POST /onboarding/mailbox` (with `action=test` the connection test runs right after the save), `POST /onboarding/mailbox/test` and `POST /onboarding/mailbox/skip` (→ the connect step). All CSRF-checked and scoped to `ctx.view`; the app password is never shown again.
- **Onboarding wizard** (`/onboarding`, `web/routes/onboarding.py` + `onboarding.html`): a stepped setup (`?step=basics|eligibility|roles|resume|answers|mailbox|linkedin`) that reuses the Profile/Settings persistence directly (imports `profile.profile_save`, `profile.resume_file_upload`, `settings.save_filters` — no duplicated logic). Basics/eligibility/standard-answers save to `users.profile`; target roles use the role-taxonomy picker (`roles.all_fields`) + locations/remote/salary and save to `users.filters` (incl. `filters.role_selection`); resume is a PDF/DOCX upload or a pasted JSON Resume. The completeness card (`profile.missing_fields`) shows what is left. "Finish setup" (`POST /onboarding/done`) sets `profile["onboarded"]=True`.
- **Per-user "Connect <site>" step** (the wizard's last step, still `?step=linkedin`): one panel per assisted site (LinkedIn, Naukri, Instahyre, Cutshort, Wellfound) with the current login chip, a file upload and a paste box, posting to `POST /onboarding/connect/{site}` (`/onboarding/linkedin` is kept as an alias). It is the same `settings.connect_user_site` flow as Settings → Sites → Connect: the export is written under the git-ignored per-user data dir (`data/cookies/<user id>/<site>.json`), imported headless via `sites.connect` → `sites.browser.import_cookies_file` (profile keyed by user id), verified by the site's `check_session` for the four non-LinkedIn sites, and reported on the user's `UserSite.login_status`. Nothing is stored in the repo and no user can see another's cookies.
- **New-user routing**: after login or signup, `base.html` shows a "Finish setup" banner linking to `/onboarding` while `ctx.needs_onboarding` is true (a nudge, not a hard redirect). `ctx.needs_onboarding` (deps.py) is false for admins and for any user with a non-empty `profile`, so existing users (demo, sai) and admins are never nagged; it stays true for a fresh signup until the profile is filled or `onboarded` is set.
