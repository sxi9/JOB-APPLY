# Dashboard

FastAPI backend, HTMX (or small React) front, dark theme, mobile friendly so queued jobs can be approved from a phone. Served behind Tailscale.

## Login
Username and password. Roles: admin (sees all users) and user (own data only).

## Home / Overview
- Stat tiles: new jobs today, queued, applied today vs cap, replies, interviews.
- Funnel: found -> matched -> applied -> replied -> interview -> offer.
- Activity feed with screenshot thumbnails.
- Collector health: each source, last run, count, red when failing.

## Jobs
- Filters: posted in last N hours, source, score, location, remote, salary, company.
- Row: title, company, location, posted time, source badge, AI match score, status chip.
- Detail: description, AI reason and missing skills, chosen resume variant, buttons Apply now / Queue / Skip / Blocklist company.

## Queue / Review
- Jobs waiting for approval (auto-submit off, or low confidence).
- Filled answers next to form fields before submit, editable cover letter. Approve / Edit / Reject.

## Applications
- Status timeline, submission screenshot, answers sent, follow-up date, reply thread.

## Inbox
- Mails classified: interview, assessment, rejection, recruiter question, spam.
- Forward to real email or WhatsApp. AI-drafted reply.

## Profile
- Resume uploads and variants, master answers vault, canned answers, skills with years.

## Settings
- Filters: keywords include/exclude, roles, locations, salary floor, seniority.
- Sites: enable per site, auto-submit on/off, daily cap, login status, re-login button that triggers the OTP flow.
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
