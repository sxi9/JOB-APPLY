# Mailbox: Gmail app password, OTP and verification mail

JOB-APPLY reads one mailbox per user. It uses it for two things:

1. **One-time codes and verification links.** When a site (Workday, Greenhouse, Naukri, ...) sends a
   login or sign-up code, the apply worker can read that code from the mailbox and continue, instead of
   pausing on you. Without a mailbox the application is paused with a clear reason ("OTP not received /
   finish it in your browser") and nothing is retried in a loop.
2. **The Inbox page.** Recruiter replies, interview invites, assessments and rejections are synced,
   classified and linked to the matching application (`jobapply sync-inbox`).

Everything goes through the same two places: **Settings → Mailbox** (the card above *Credentials*) and
the **Mailbox** step of the onboarding wizard. Both call the same code (`mail/mailbox.py`), so saving in
one shows up in the other.

## Gmail: create an app password

Google no longer accepts your normal password for IMAP/SMTP. You need a 16-character *app password*,
which only exists once 2-Step Verification is on.

1. Open **myaccount.google.com** and sign in with the Gmail account you want JOB-APPLY to read.
2. In the left menu click **Security**. Under *How you sign in to Google* find **2-Step Verification**.
   If it says *Off*, click it and follow the prompts (a phone number or an authenticator app). You cannot
   continue without this.
3. Back on the Security page, open **2-Step Verification** again and scroll to the bottom: there is a
   row called **App passwords**. (If it is missing, search for "App passwords" in the search bar at the
   top of the account page, or open `myaccount.google.com/apppasswords` directly.) Google Workspace
   accounts may have this hidden by the admin; ask them to allow "less secure apps / app passwords".
4. On the App passwords page type a name such as `JOB-APPLY` in the *App name* box and click **Create**.
5. A yellow box shows the password as four groups of four letters (`abcd efgh ijkl mnop`). Copy it now;
   Google never shows it again. Spaces are fine, the app strips them.
6. In JOB-APPLY, Settings → Mailbox: provider **Gmail**, your address, paste the app password, click
   **Save and test**. The status chip turns **ok** and the subject of your newest mail is shown as proof
   that the login worked.

Deleting the app password on the Google page revokes JOB-APPLY's access instantly; nothing else about
your account changes.

### Outlook / Hotmail / Microsoft 365

Personal accounts: **account.microsoft.com → Security → Advanced security options → App passwords**
(also needs two-step verification). Work accounts often have IMAP disabled by the tenant admin; the
test will say so. Hosts are preset (`outlook.office365.com:993`, `smtp.office365.com:587`).

### Yahoo

**Account Security → Generate app password** (hosts preset: `imap.mail.yahoo.com:993`,
`smtp.mail.yahoo.com:465`).

### Any other mailbox

Choose **Custom** and fill the IMAP (SSL, usually 993) and SMTP (STARTTLS 587 or SSL 465) hosts under
*Advanced*.

## What the test does

"Test connection" (and `jobapply mail test -u <user>`):

- logs in over IMAP with the saved address and app password,
- selects INBOX and fetches **only the header** of the newest message (`BODY.PEEK[HEADER]`, so the mail
  is not marked read), and shows its subject line in the UI,
- logs in over SMTP (STARTTLS or SSL) and disconnects **without sending anything**,
- writes `last_test_at`, `status` (`ok` / `imap_failed` / `smtp_failed`) and the failure reason to
  `users.profile["mailbox"]`. The password is never part of that record.

Common failures and what they mean:

| Message | Fix |
| --- | --- |
| login rejected: use an app password ... | You pasted the account password, or 2-Step Verification is off. Create an app password. |
| login rejected: wrong address or app password | Typo in the address, or the app password was revoked. Create a new one. |
| host not found | Wrong host under *Advanced*; pick the provider preset again. |
| connection timed out | Port 993/587 blocked by a firewall, or IMAP disabled by a Workspace admin. |

## What is stored, and where

| What | Where |
| --- | --- |
| Address, IMAP host/port, app password | Vault row `site="mailbox", kind="imap"`, encrypted with `JOBAPPLY_VAULT_KEY` |
| Address, SMTP host/port, app password | Vault row `site="mailbox", kind="smtp"`, same key |
| Provider, hosts, last test time, status, reason, newest subject | `users.profile["mailbox"]` (plain JSON, no secret) |
| Codes already consumed | `app_settings` key `otp_used:<user id>`: salted SHA-256 digests + timestamp, 24 h expiry. Never the code itself. |

The password is never shown again after saving (the field says "saved, never shown"), never written to
logs, never included in a flash message, and never sent anywhere except to the IMAP/SMTP hosts you
configured. "Remove mailbox" deletes both vault rows and the profile entry.

## What the app does with the mailbox

Reads:

- `jobapply sync-inbox` reads recent mail (last 14 days, then incrementally) to fill the Inbox page.
- The apply worker, when a site asks for a code, calls `mail.otp.wait_for_code(reader, sender=...,
  after=<when the code was requested>)`: it polls every few seconds (up to the timeout, typically two
  minutes), accepts only mail that arrived **after** the request (2 minutes of clock slack), from the
  expected sender (`workday`, `greenhouse`, `naukri`, `noreply`, ... presets or a domain / regex),
  extracts the code and records it as used so the same code is never typed twice. On timeout the
  application is paused with "OTP not received"; it does not loop.
- `jobapply mail wait-code -u <user> --sender workday --timeout 120` does the same from the CLI and
  prints only that a code was found and how many digits it has (`--show` prints the code).

Code extraction (`mail.otp.extract_code`) understands "Your code is 482913", "482913 is your code",
"security code 123 456", "12-34-56", "G-123456" and HTML-only mails, and ignores phone numbers,
order / ticket / reference numbers, dates, times, years and durations.

## What it never does on its own

- It never **sends** mail from the mailbox outside the gated paths you can see: replying from the Inbox
  page is a button you click; forwarding needs a forward address you set; follow-up chasers are capped
  and only go to recruiters who wrote first. Nothing is sent while the global dry-run switch is on.
- It never **deletes**, moves or archives mail. Inbox sync marks nothing; the connection test uses
  `PEEK` so even the "seen" flag is untouched.
- It never reads mail older than the request when waiting for a code, and never reuses a code.
- It never stores or prints the app password; the UI only ever shows the address and the host.
- It never creates accounts, submits forms or clicks a final button because a mail arrived: those steps
  still go through the kill switch, the global dry-run switch, per-site auto-submit (off by default),
  the daily caps and active hours.

## CLI

```
jobapply mail test -u sai                         # IMAP login + newest subject, SMTP login, no send
jobapply mail wait-code -u sai --sender workday --timeout 120
jobapply mail wait-code -u sai --sender noreply --subject verify --since-minutes 5 --show
```

`wait-code` exits 0 when a code arrived, 1 on timeout, 2 when the user or mailbox is missing.
`--allow-reuse` disables the used-code registry (debugging only).
