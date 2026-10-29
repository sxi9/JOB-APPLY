# Form profiling

Three layers, from most to least deterministic.

## 1. Known ATS, hand-written profiles

Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, Naukri, LinkedIn Easy Apply. Each has a YAML profile mapping selectors to vault keys.

```yaml
ats: greenhouse
detect: "boards.greenhouse.io|job-boards.greenhouse.io"
fields:
  "input[name=first_name]": profile.first_name
  "input[name=last_name]": profile.last_name
  "input[name=email]": profile.email
  "input[name=phone]": profile.phone
  "input[type=file][name=resume]": resume.pdf_for(job)
  "select[name*=work_auth]": answers.work_authorization
submit: "button[type=submit]"
```

Fast, reliable, no LLM involved.

## 2. Learned profiles, built automatically

On an unknown form the AI reads the accessibility tree once, maps each field to a vault key, and the mapping is saved keyed by domain plus form fingerprint. Next time any user hits the same form it runs deterministically. Profiles hold field-to-key mappings only, no personal data, so they are shared across users.

## 3. Per-question answer bank

Custom questions repeat across companies ("Why do you want to work here", "Years of experience with Python", "Willing to relocate"). Each unique question is stored by normalized hash. First time the AI drafts an answer from the user's vault and the user can edit it. After that the same question is answered instantly from the user's bank. The dashboard lists unanswered questions so they can be filled in bulk.

## Custom dropdowns and autocompletes

Greenhouse job boards, Ashby and Workday render "Select..." questions as a text input with `role="combobox"`
(React-Select) instead of a `<select>`. The extractor classifies these as type `combobox`, resolves their label
from `aria-labelledby` or the nearest label, and pairs each with its twin (React-Select's hidden required input,
or an ATS `question_<n>` text box mirrored by an unlabelled combobox); the twin is marked hidden so it is never
filled twice or reported as unfilled. Filling one opens it empty first (a fixed list shows its options, an
autocomplete shows nothing), types the value, and picks the option that matches exact > starts-with > fuzzy
(rapidfuzz, 80), verifying the control shows the chosen text. A value no fixed option matches goes back to the
answer source together with the options read from the dropdown (so yes/no questions are answered by the
heuristics, the answer bank or the LLM); an autocomplete (city, school lookups) takes the suggestion matching
the value's first token, else the first suggestion, which is flagged for review.

## Navigator: any external link gets a real attempt

An apply URL on a host that is not a known ATS used to be paused ("apply manually"). `forms/navigator.py`
(`Navigator`, called by `FormRunner` whenever the landing page has no usable form) now finds the way to the
application form, and the pipeline routes unknown hosts through it (`route_external_result`, the LinkedIn
offsite loop) under a synthetic site key `external` (`users.SITES`: daily cap 10, auto-submit off; the row is
created lazily for older accounts by `pipeline.apply.external_site`).

What it does, in order, per hop (at most 6 hops, 120 s in all):

1. **Cookie banners** are dismissed (containers whose id/class says cookie / consent / gdpr; the least permissive
   button is preferred: reject / necessary only, then accept / got it).
2. **Captcha** on the page pauses the application with `captcha on page (<selector>)`.
3. A form embedded in an **iframe** is opened directly (the frame URL is loaded in the page, preferring one a
   built-in profile recognises), so filling, screenshots and submit work on a plain page.
4. A **sign-in / create-account wall** (a visible password input, few other inputs) is handled generically:
   - a stored account for the host (vault site `ext:<host>`, kind `login`) is used to sign in, also in a dry run;
   - otherwise an account is created **only when the run may submit** (global dry run off AND the `external`
     site's auto-submit on): the profile email plus a password generated per host (`generate_password`), stored
     in the vault *before* the sign-up form is sent (never the mailbox password, never printed), then the
     verification **code or link is read from the user's mailbox** (`mail.otp.wait_for_code` / `wait_for_link`
     over the vault `mailbox` IMAP credential). No mailbox: paused with `mailbox not configured`; no mail in time:
     paused with `no verification code arrived`; dry run / auto-submit off: paused with `needs an account; not
     created`.
5. **Candidates**: every visible link / button is scored on its text, aria label, href and data-* attributes:
   `Apply`, `Apply now`, `Start application`, `Continue to application` (100), anything containing *apply* (80),
   `Next` / `Continue` (60). Share / save / print / privacy / search / home and the like score 0, and so does any
   control that looks **final** (`Submit`, `Send application`, `Finish`, `Confirm`...) and a form's own submit
   button (a two-field quick-apply form that says "Apply" is filled, never clicked through).
6. When no candidate scores at least 50, the **LLM** (`ai.service.structured`, task `navigate`, prompt
   `NAVIGATE_SYSTEM`) picks from a **numbered list** of the safe candidates; the answer is an index into that
   list (`NavPick.choice`, 0 = none), never free text, and final-looking controls are not in the list. No LLM,
   a `FakeLLM` without a canned `navigate` answer, a provider failure or an out-of-range index all mean
   "heuristics only". As a last resort a `Sign in` / `Create account` link is followed so the wall handling above
   can take over.
7. The pick is **followed**: a real href is loaded in the same tab (so `target=_blank` links work too); anything
   else is clicked, and a popup it opens is taken over (its URL loaded here, the popup closed). A `mailto:` apply
   link is not followed: the result carries it and the pipeline applies **by email** (`apply_by_email`: the
   tailored resume attached, `application_message` as the body; sent only when not dry run AND the `email`
   site's auto-submit is on AND the mailbox has SMTP settings; otherwise the full `.eml` draft is written to
   `data/screenshots/<user_id>/` and the application is paused for review).

After the form is found and filled, **multi-step wizards** are walked (`FormRunner._wizard`): a `Next` /
`Continue` control (never a final one) is clicked up to 4 times and the new fields of each step are planned
(profile -> learned -> AI mapper) and filled like the first. In a dry run the submit guard still blocks a `Next`
that posts the form, so a server-side wizard ends at its first step with a screenshot; a JS wizard advances.

Everything the navigator could not do ends as a **paused** application with the specific reason (`no apply
control found on <url>`, `no application form found after N hops`, `captcha on page`, `<host> needs an
account; ...`), never a silent failure and never a loop. The hops taken are recorded on the result (`hops`) and
logged.

## Verification

- Dry-run mode fills the form, screenshots it, does not submit. Reviewed on the Queue page.
- After a few clean dry runs on a site, the user flips it to auto-submit.
- If a site changes its form, field match rate drops, the run fails safely, falls back to the AI mapper and re-learns the profile.
- Every real submission stores a screenshot and the exact answers sent.
- CAPTCHA or low confidence pauses the run and notifies the user (WhatsApp).
