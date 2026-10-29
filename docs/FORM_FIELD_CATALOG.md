# Form field catalog

What the five built-in ATS application forms actually ask, from a live field audit on **2026-10-29**. For
each form the real `FormRunner` was run headless (demo vault, FakeLLM, `submit=False`, DB rolled back) and the
raw `extract_fields()` list was dumped after `FormRunner._locate_form` hopped any Apply button or embedded
frame.

Forms audited:

| ATS | Live posting | Reached via | Anti-bot |
|-----|--------------|-------------|----------|
| Greenhouse | Stripe `8265686` | Apply link -> `/embed/job_app` frame | reCAPTCHA |
| Lever | Palantir `655f9937…` | page itself | hCaptcha |
| Ashby | Ramp `8f1aa562…` | page itself | reCAPTCHA |
| Workable | Hugging Face `0BD8C06DB3` | page itself | none seen |
| SmartRecruiters | Experian `744000014964005` | **blocked** | DataDome bot wall |

Notes:
- CAPTCHAs (Greenhouse reCAPTCHA, Lever hCaptcha, Ashby reCAPTCHA) are expected and the runner pauses on them;
  the fields are still catalogued.
- **SmartRecruiters could not be audited live.** The public posting is fronted by a DataDome bot wall
  (`geo.captcha-delivery.com`) that serves zero form fields to headless automation. Bypassing bot detection
  is out of scope, so the SmartRecruiters rows below come from its standard one-click form (the shape the
  dashboard fixture encodes), not a live capture.
- "Mapped to" is the key a built-in profile assigns the field. "custom question" = handled by the answer bank
  / LLM (no stable vault key, or a company-specific screening prompt we deliberately do not hardcode).

---

## Greenhouse (Stripe) — 49 visible fields, origin `builtin`

Standard fields carry fixed ids; screening questions carry generated `question_<n>` ids and are matched by
label. Country and city are React-Select comboboxes; the work-eligibility questions are Yes/No comboboxes.

| Field (label) | Type | Required | Mapped to |
|---|---|---|---|
| First Name | text | yes | `profile.first_name` |
| Last Name | text | yes | `profile.last_name` |
| Email | text | yes | `profile.email` |
| Country | combobox | yes | `profile.location` |
| Phone | tel | yes | `profile.phone` |
| Location (City) | combobox | yes | `profile.location` (**newly mapped** `#candidate-location`) |
| Resume (Attach) | file | no | `derived.resume_path` |
| Cover letter (Attach) | file | no | skip (file upload; text version answers a question) |
| Please select the country where you currently reside | combobox | yes | custom question |
| Country/countries you anticipate working in | checkbox (multi, ~40 options) | yes | custom question |
| Are you authorized to work in the location(s) selected | combobox (Yes/No) | yes | custom question |
| Will you require Stripe to sponsor you for a work permit | combobox (Yes/No) | yes | `derived.needs_sponsorship` (**newly mapped** `label=sponsor`) |
| If remote, do you plan to work from the posted location | combobox | yes | custom question |
| Have you ever been employed by Stripe or an affiliate | combobox (Yes/No) | yes | custom question |
| Who is your current or previous employer | text | yes | custom question (maps well to `profile.current_company`) |
| What is your current or previous job title | text | yes | custom question (maps well to `profile.current_title`) |
| If located in the US, what city and state | text | yes | custom question |
| Gender | combobox | no | `profile.gender` |
| Are you Hispanic/Latino | combobox | no | skip |
| Veteran Status | combobox | no | `profile.veteran_status` |

Greenhouse also injects a hidden `g-recaptcha-response` textarea (in the profile `never` list) and a search
box (skipped).

---

## Lever (Palantir) — 88 visible fields, origin `builtin`

The top block is Lever's standard form (stable `name=` attributes). Everything under it is a per-posting
custom card (`cards[<uuid>][fieldN]`), so those are matched only by label / answer bank, never hardcoded. The
EEO block at the bottom uses stable `eeo[...]` select names.

| Field (label) | Type | Required | Mapped to |
|---|---|---|---|
| Resume / CV | file | yes | `derived.resume_path` |
| Full name | text | yes | `derived.full_name` |
| Email | email | yes | `profile.email` |
| Phone | text | no | `profile.phone` |
| Current location | text | yes | `profile.location` |
| Current company | text | no | `profile.current_company` |
| LinkedIn URL | text | no | `profile.linkedin` |
| GitHub URL | text | no | `profile.github` |
| Portfolio URL | text | no | `profile.portfolio` |
| Languages you speak | checkbox (multi, ~40 options) | yes | custom question |
| Internship availability windows | checkbox (multi, date ranges) | no | custom question |
| Eligibility attestations (1-year, studying, proficient) | checkbox group | yes | custom question |
| Target graduation month / year | select | yes | custom question |
| Role preference (FDSE Delta / Dev / Other) | select | yes | custom question |
| Yes/No screening questions (several) | radio (Yes/No) | yes | custom question |
| University | select (long list) | yes | custom question |
| Graduation year | select | yes | custom question |
| Essay / long-answer prompt | textarea | yes | custom question |
| How did you hear about us | select | yes | custom question |
| Data-use consent | radio (consent / no consent) | yes | `const:Yes` |
| Additional information | textarea | no | `derived.cover_letter` |
| Veteran status | select | no | `profile.veteran_status` (**newly mapped** `eeo[veteran]`) |
| Disability status | select | no | `profile.disability_status` (**newly mapped** `eeo[disability]`) |

---

## Ashby (Ramp) — 11 visible fields, origin `builtin`

Besides the `_systemfield_*` inputs each field has a per-posting uuid, so most are matched by label.

| Field (label) | Type | Required | Mapped to |
|---|---|---|---|
| Autofill from resume | file | no | (ignored convenience upload) |
| Legal Name | text | yes | `derived.full_name` |
| Email | email | yes | `profile.email` |
| Phone | tel | yes | `profile.phone` |
| SMS consent | radio (Yes/No) | no | skip |
| WhatsApp consent | radio (Yes/No) | no | skip |
| Location autocomplete ("Start typing…") | combobox | no | custom (unlabelled places lookup) |
| Resume | file | yes | `derived.resume_path` |
| LinkedIn Profile | text | yes | `profile.linkedin` |

This particular Ramp posting is short; other Ashby forms expose sponsorship / location / cover-letter fields,
which the profile already maps by label (`label=sponsorship`, `label=Location`, `label=Cover Letter`, etc.).

---

## Workable (Hugging Face) — 13 visible fields, origin `builtin`

Workable's built-in fields keep stable `name=` attributes. The **resume upload input has a generated
name/id** (`input_files_input_<rand>`), which is why the old `input[name=resume]` selector never matched — it
is now matched by `input[type=file]`. LinkedIn/GitHub here are per-posting custom fields (`CA_<n>`), and the
long prompts are screening questions (`QA_<n>`).

| Field (label) | Type | Required | Mapped to |
|---|---|---|---|
| First name | text | yes | `profile.first_name` |
| Last name | text | yes | `profile.last_name` |
| Email | email | yes | `profile.email` |
| Phone | tel | no | `profile.phone` |
| Summary | textarea | no | `profile.summary` |
| Resume | file | yes | `derived.resume_path` (**fixed** `input[type=file]`) |
| Github profile | textarea | yes | custom question (per-posting `CA_`) |
| Linkedin | textarea | yes | custom question (per-posting `CA_`) |
| Expected salary | text | no | custom question (maps well to `profile.expected_ctc`) |
| Notice period / availability | combobox | no | custom question (maps well to `profile.notice_period`) |
| How did you hear about us | text | no | custom question |
| Why are you applying to work at … | textarea | yes | custom question |
| Which project would you be most excited to work on | textarea | yes | custom question |

---

## SmartRecruiters (Experian) — standard form (live posting bot-walled)

Not captured live (DataDome bot wall). These are the standard one-click apply fields. The resume upload is a
file widget, now matched by `input[type=file]` rather than an assumed name; the rest remain **best-effort and
unverified** until a real dry-run screenshot can be reviewed locally.

| Field (label) | Type | Required | Mapped to |
|---|---|---|---|
| First name | text | yes | `profile.first_name` |
| Last name | text | yes | `profile.last_name` |
| Email | email | yes | `profile.email` |
| Phone number | tel | no | `profile.phone` |
| Location | text | no | `profile.location` |
| Resume | file | yes | `derived.resume_path` |
| LinkedIn profile | url | no | `profile.linkedin` |
| Notice period (screening) | text | yes | custom question (maps well to `profile.notice_period`) |
| Privacy policy consent | checkbox | yes | `const:Yes` |

---

## Inputs the system needs from the user

The union of distinct vault keys these forms require. This is the canonical list of what to collect from a
user before applying; the machine-readable version lives in `src/jobapply/forms/required_inputs.py`.

### Essential — (almost) every form blocks without these

- **First name** — `profile.first_name`
- **Last name** — `profile.last_name`
- **Full name** — `derived.full_name` (Lever and Ashby use a single name field)
- **Email** — `profile.email`
- **Phone** — `profile.phone`
- **Current location / city** — `profile.location`
- **Resume / CV file** — `derived.resume_path`

### Common — frequently required or asked

- **LinkedIn URL** — `profile.linkedin` (required as a custom field on some forms, e.g. HF/Workable, Ashby)
- **GitHub URL** — `profile.github`
- **Portfolio / website URL** — `profile.portfolio` / `derived.links`
- **Current / previous employer** — `profile.current_company`
- **Current / previous job title** — `profile.current_title`
- **Total years of experience** — `profile.years_experience`
- **Highest education** — `profile.education`
- **Professional summary** — `profile.summary` (Workable)
- **Cover letter** — `derived.cover_letter`
- **Work authorization / right to work** — `profile.work_authorization`
- **Needs visa sponsorship (yes/no)** — `derived.needs_sponsorship`
- **Willing to relocate (yes/no)** — `derived.willing_to_relocate`
- **Notice period / earliest start** — `profile.notice_period`
- **Current salary / CTC** — `profile.current_ctc`
- **Expected salary / CTC** — `profile.expected_ctc`

### EEO self-identification (voluntary; Greenhouse + Lever expose them as standard fields)

- **Gender** — `profile.gender`
- **Veteran status** — `profile.veteran_status`
- **Disability status** — `profile.disability_status`

### Recurring custom screening questions (answer bank)

These repeat across companies and are drafted once, then reused from the answer bank. The ones with a vault
key can be auto-answered; the rest are genuinely per-posting:

| Question | Answered from |
|---|---|
| Do you require visa sponsorship? | `derived.needs_sponsorship` |
| Are you legally authorized to work here? | `profile.work_authorization` |
| Current country of residence | `profile.location` |
| Willing to relocate? | `derived.willing_to_relocate` |
| Notice period / earliest start | `profile.notice_period` |
| Current / expected salary | `profile.current_ctc` / `profile.expected_ctc` |
| Years of experience | `profile.years_experience` |
| Highest education | `profile.education` |
| Current / previous employer and title | `profile.current_company` / `profile.current_title` |
| Previously employed by this company? | free (per-posting) |
| How did you hear about this role? | free (per-posting) |
| Why do you want to work here? | free (per-posting) |
