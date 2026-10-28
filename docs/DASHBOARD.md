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
