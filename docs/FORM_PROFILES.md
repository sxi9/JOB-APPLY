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

## Verification

- Dry-run mode fills the form, screenshots it, does not submit. Reviewed on the Queue page.
- After a few clean dry runs on a site, the user flips it to auto-submit.
- If a site changes its form, field match rate drops, the run fails safely, falls back to the AI mapper and re-learns the profile.
- Every real submission stores a screenshot and the exact answers sent.
- CAPTCHA or low confidence pauses the run and notifies the user (WhatsApp).
