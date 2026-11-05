# Notifications: WhatsApp alerts, the apply mode, group feeds and the subscriber bot

Code: `notify.py` (bridge transport, targets, groups, health), `pipeline/alerts.py` (new-job and outcome
alerts, the per-user mode), `web/routes/settings.py` (the Notifications card, the test button),
`cli_alerts.py` (`jobapply alerts ...`), `pipeline/feeds.py` + `cli_feeds.py` (admin group feeds),
`bot/whatsapp.py` + `bot/dispatch.py` + `bot/groups.py` + `cli_bot.py` (the subscriber bot, last section).
Tests: `tests/test_alerts.py`, `tests/test_notify.py`, `tests/test_feeds.py`, `tests/test_bot.py`,
`tests/test_bot_dispatch.py`, `tests/test_web.py`.

## The bridge

The server talks to the `whatsapp-headless` bridge (Express, `JOBAPPLY_WHATSAPP_BASE_URL`, default on the
box `http://127.0.0.1:3001/api`, every route with `Authorization: Bearer $JOBAPPLY_WHATSAPP_TOKEN`):

| call | used for |
| --- | --- |
| `POST /send` `{"to": "<number or group jid>", "text": "..."}` | every alert (`WhatsAppNotifier.send_text`) |
| `GET /groups` -> `[{name, jid, ...}]` | the group picker on Settings (`list_groups()`, cached 10 minutes, 60 s when empty, 5 s timeout, `[]` + a log line on any error) |
| `GET /health` -> `{status: "open", me: "91...@s.whatsapp.net"}` | the status chip (`bridge_health()`: `ok` only when the status is `open`; the number is masked to its last 4 digits before it is shown or logged) |

Without `JOBAPPLY_WHATSAPP_BASE_URL` the notifier is log-only: every alert is written to the log, nothing is
sent, the chip says *not configured* and the group list is empty. WhatsApp renders `*bold*`; a bare `http(s)`
URL alone on its line is clickable, so the messages below never put punctuation after a link.

`JOBAPPLY_PUBLIC_URL` (default `http://192.168.29.22:8010`) is the dashboard address used for the
`📋 .../jobs/<id>` links in every alert.

## Targets

Per user, on Settings -> Notifications (`users.preferences`):

- `whatsapp_number`: the user's own number with country code.
- `whatsapp_group_jid`: a group the **bridge's** WhatsApp account is in (`120363...@g.us`). The bridge is
  the owner's own phone, so its group list is not public (`settings.allowed_groups`): an **admin** sees every
  group from `GET /groups` (for their own card or while *viewing as* another user) and may save any jid; a
  **plain user** sees, and may pick, only the groups an admin shared on `/admin` -> **Shared WhatsApp
  groups** (`app_settings["shared_group_jids"]`, comma-separated jids; a multi-select of the bridge's groups
  plus a paste field for when the bridge list is empty; `switches.shared_group_jids`), plus the group already
  saved for them (an admin may have picked it while viewing as them), so a save never loses it.
  `save_preferences` refuses any other jid from a plain user (`settings.group_allowed`): the saved group is
  kept and *That WhatsApp group is not shared with you, so the saved group was kept* is flashed. Without a
  shared or saved group the card shows a read-only *none* and the **Send to** radio offers the number only.
  The saved jid stays selected even when the list cannot be fetched (bridge down), so a save while the
  bridge is unreachable never silently clears the group.
- `alerts_target`: `number` (default) | `group` | `both`.

`notify.alert_targets(user)` turns these into the list of targets an alert goes to. The older alerts
(`notify(session, user, kind, message)`: interview, assessment, paused, ...) still go to the number only, with
the `[JOB-APPLY]` prefix, filtered by `alert_types`.

## Mode: notify-only vs automatic

`preferences.apply_mode`, radio on the Notifications card:

- **`notify`** (the code default for every user without the preference, so every new sign-up and any
  existing account that has not chosen yet, including `sai` and the admin; no data was migrated): the
  pipeline collects, filters and scores for the user and posts the matches. **Nothing is ever filled or
  submitted.** Every apply entry point asks `pipeline.alerts.notify_only_skip(user, where)` first and returns
  empty: `pick_applications`, `apply_user`, `pick_external_linkedin_applications`,
  `apply_external_linkedin_user`, `apply_assisted_user` (Naukri, Instahyre, Cutshort, Wellfound, Workday),
  the site runners `apply_linkedin_user` (before the profile is launched), `apply_naukri_user`,
  `apply_instahyre_user`, `apply_cutshort_user`, `apply_workday_user`, and every site picker (`pick_linkedin_applications`,
  `pick_workday_applications`, `pick_naukri_applications`, `pick_instahyre_applications`,
  `pick_cutshort_applications`, `pick_wellfound_applications`).
  The skip is logged once per user and path every 10 minutes, not per picker call. The Queue page shows the
  banner *Notify-only mode: nothing is applied for you; switch to automatic in Settings*; it still lists the
  scored / queued rows so the user sees what would be applied to.
- **`auto`**: the historical behaviour, every gate intact (kill switch, global dry run, site enabled and
  auto-submit, per-user and per-site daily caps, active hours, challenge cooldowns), plus one **outcome
  alert** per application (below).

Switching to automatic is the user's own choice on their Settings page; an admin can do it through
*View as*.

## New-job alerts

`pipeline.alerts.notify_new_jobs(session, user, *, now=None, dry_run=False) -> list[str]`, run for every active
user at the end of `collect.run_all` (after `assign_jobs`, inside a try/except per user and around the
whole hook: a bridge or notifier error is a warning, never a failed collect), and from the CLI.

- Off by default: `new_jobs_enabled` must be ticked (*New job alerts* on the card).
- Candidates, per target: the user's `Application` rows (any status) created in the last 24 hours
  (`NEW_JOB_WINDOW`, so enabling the alerts does not replay the history) that have **no** `ApplicationEvent`
  of kind `notified` for that target (`detail` `whatsapp:number` / `whatsapp:group`), with
  `score >= new_jobs_min_score` (default 0; an unscored job passes only when the minimum is 0, so set a
  minimum when you want scored jobs only).
- Newest posted first (`posted_at`, else `first_seen_at`), batches of at most **8** jobs per message, at most
  **5** messages per target, run and user (the rest is logged as dropped and goes out next run).
- One message:

  ```
  🆕 3 new jobs — 14:05 IST, 05 Nov

  *Senior Backend Engineer* — Acme Inc · Pune
  🕒 posted 12 min ago (13:53 IST, 05 Nov) · via greenhouse
  https://apply.example/1
  📋 http://192.168.29.22:8010/jobs/42

  *Next job* — ...
  ```

  Title capped at 90 characters with markup characters stripped (so the bold never breaks), company and
  location (`Remote` when the job is remote and has no location), relative age (`just now`, `12 min ago`,
  `3 h ago`, `2 d ago`) plus the IST time (`zoneinfo` Asia/Kolkata), the source, the apply URL alone on its
  line (`url` when there is no `apply_url`; a `mailto:` link is shown as *apply by email: address*), then the
  dashboard link, then a blank line.
- **Never re-posted.** A batch is *claimed* before the send: an `ApplicationEvent(kind="notified",
  detail="whatsapp:number" | "whatsapp:group")` is written and committed for every job of the batch and
  that target (unique per application and target, index `ux_application_events_notified`, migration
  `0006`), so a second run in the same window (`collect` and `collect_hot` are two jobs of one scheduler
  process, which also serialises the alert / feed phase with a lock; `jobapply alerts send-new` by hand) hits
  the unique index and skips the batch instead of posting it again. On a failed send the claim is deleted
  again and the batch is retried next run **for that target only**: with `both`, a job the number received
  but the group did not goes to the group alone next time. The event never carries the number or jid (the
  dashboard History shows `whatsapp:number`; the home activity feed leaves `notified` rows out). A status
  change later (queued, applied, ...) does not make a job new again.
- `dry_run=True` returns the messages and neither sends nor writes.

## Outcome alerts (automatic mode)

`pipeline.alerts.notify_outcome(session, user, app, job, status, detail)` is called by
`pipeline.apply.record_outcome`, the one place every filler (ATS form runner, LinkedIn Easy Apply and offsite,
the assisted sites, Workday, apply by email) books its result. It never raises. Only for users in `auto`
mode with a target:

| status | text |
| --- | --- |
| applied | `✅ applied` |
| dry_run / queued | `📝 queued for your approval` |
| paused | `⏸ paused: <reason>` |
| failed | `❌ failed: <reason>` |

followed by `*<title>* — <company>` and the `📋` dashboard link; an `ApplicationEvent(kind="outcome")` is
recorded. When the outcome alert was delivered, the older `[JOB-APPLY] <job>: <reason>` paused alert to the
number is not sent as well (no duplicate); users in notify mode or without a target keep the older alert.

## Settings card

Settings -> Notifications (`POST /settings/preferences`, `section=notifications`): the bridge chip
(`open` + masked account number, or the failure status), the **Mode** radio with one-line explanations,
WhatsApp number, the **WhatsApp group** select (every bridge group for an admin, with the *bridge is
unreachable* hint; the admin-shared groups for a plain user, see *Targets*),
**Send to**, forward-to email, **New job alerts** + **Minimum score**, the alert-type checkboxes. Invalid
values fall back (`notify`, `number`, score clamped to 0..100).

**Send test** (`POST /settings/notifications/test`, CSRF, scoped to the viewed user): one short message
(`✅ JOB-APPLY test for <user> — <time>. Mode: ...`) to each saved target, result flashed. Disabled until a
target is saved; save the card first, the button uses the saved preferences.

## CLI

```
jobapply alerts send-new -u <user> [--dry-run] [--force]   # --dry-run prints the messages, sends and records nothing
jobapply alerts groups                                      # bridge status + table of group name and jid
jobapply alerts test -u <user>                              # one test message to the saved target(s)
```

`send-new` refuses (exit 1) when the user's alerts are off; `--dry-run --force` previews anyway without
saving anything. `groups` exits 1 when the list is empty (bridge down or not configured).

## Group feeds

`pipeline/feeds.py`, `cli_feeds.py` (`jobapply feeds ...`), the **Group feeds** card on `/admin`
(`web/routes/admin.py`), tests in `tests/test_feeds.py` and `tests/test_web.py`.

A feed posts new jobs to a WhatsApp **group** the bridge's account is in. Nobody in the group needs a
JOB-APPLY account: the messages carry the apply link only (no `📋` dashboard link). Feeds are configured by an
admin, not per user, and stored as a JSON list in `app_settings["group_feeds"]`:

```
{"id": "3f9a1c2b", "name": "Security jobs", "group_jid": "120363...@g.us", "enabled": true,
 "max_per_run": 10,
 "filters": {"role_selection": ["cybersecurity:*"], "roles": [], "keywords_include": [],
             "keywords_exclude": ["intern"], "locations": ["remote", "Pune"], "remote_only": false,
             "max_age_hours": 24, "sources": [], "min_salary": null}}
```

- **Same matcher as the users.** `feeds.feed_filters(feed)` turns the filter dict into the users'
  `pipeline.filters.UserFilters` (`min_salary` is the users' `salary_floor`; `role_selection` takes the
  career-field picker keys `field:*` / `field:sub`, `roles` the free-text roles) and
  `pipeline.filters.match(job, filters)` decides, so a feed sees exactly what a user with those filters
  would be assigned. `max_age_hours` defaults to 24 (`0` = any age, at most 720).
- **When it runs.** `feeds.run_feeds(session, new_job_ids)` at the end of every `collect.run_all`, right
  after the user alerts, inside its own try/except (a feed or bridge problem is a warning, never a failed
  collect); `jobapply feeds run` and the admin *Post now* button run it on the jobs first seen inside the
  feed's age window instead. Disabled feeds are skipped.
- **Never twice.** Every posted job is recorded in the `feed_posts` table (`FeedPost(feed_id, job_id,
  posted_at)`, unique per feed and job, migration `0005`) **after** the send succeeded; a failed send
  records nothing and the batch is retried next run. Removing a feed removes its rows. The *Posted* count on
  the admin page is the number of those rows and *Last run* lives in `app_settings["group_feed_runs"]`
  (`{feed_id: iso}`, written by `run_feeds` only): a run never rewrites `group_feeds` itself, so an admin
  edit (disable, remove, new filters) made while a run is posting stands.
- **Batches and caps.** Newest posted first, at most **8** jobs per message (the alerts' `BATCH_SIZE`) and
  at most `max_per_run` jobs per run and feed (default 10, hard ceiling 50); the rest goes out next run.
- One message (the user-alert job block without the dashboard line):

  ```
  📢 Security jobs — 2 new jobs — 14:05 IST

  *SOC Analyst* — Acme Inc · Pune
  🕒 posted 12 min ago (13:53 IST, 05 Nov) · via greenhouse
  https://apply.example/1

  *Security Engineer* — ...
  ```

### Admin card

`/admin` -> **Group feeds** (admin only, CSRF on every POST): the bridge chip, the table of feeds (name, id,
group name + jid, filter summary, enabled, last run, posted count) with per-row buttons, and the add / edit
form.

| action | route |
| --- | --- |
| add / save (`id` present) | `POST /admin/feeds` — name, group (select from `list_groups()` or a pasted jid, the pasted one wins), free-text roles, include / exclude keywords, locations, sources, posted-within hours, max per run, minimum salary, remote only, enabled, and the career-field picker (`_role_picker.html`, the same markup as Settings) |
| Edit | `GET /admin?feed=<id>` prefills the form |
| Run now (dry run) | `POST /admin/feeds/<id>/preview` — renders the page with the messages that would go out (works for a disabled feed too); nothing sent, nothing recorded |
| Post now | `POST /admin/feeds/<id>/run` — `run_feeds` for that feed, result flashed |
| Enable / Disable / Remove | `POST /admin/feeds/<id>/enable`, `/disable`, `/remove` |

### CLI

```
jobapply feeds list                                    # id, on/off, name, group, filters, last run, posted
jobapply feeds run [--dry-run] [--feed <id>]           # post (or print) each enabled feed's new matches
jobapply feeds add --name "Security jobs" --group 120363...@g.us \
    [--field cybersecurity | --field cloud:security ...] [--role "security engineer" ...] \
    [--include kw ...] [--exclude kw ...] [--location remote ...] [--source greenhouse ...] \
    [--hours 24] [--remote-only] [--min-salary N] [--max-per-run 10] [--disabled]
jobapply feeds enable <id> | disable <id> | remove <id>
```

`--field` takes a career-field key (`cybersecurity`, expanded to `cybersecurity:*`) or `field:subrole`;
an unknown key exits 2 with the list of known ones. Group jids come from `jobapply alerts groups`.

## Subscriber bot

`bot/whatsapp.py` (the conversation engine), `bot/dispatch.py` (posting), `bot/groups.py` (group commands),
`web/routes/whatsapp.py` (the webhook), `cli_bot.py` (`jobapply bot ...`), the **Subscribers** card on
`/admin`, migration `0007` (`subscribers`, `subscriber_posts`). Tests: `tests/test_bot.py`,
`tests/test_bot_dispatch.py`, `tests/test_web.py`.

Anyone can get job posts by WhatsApp **without a JOB-APPLY account**: they DM the bridge's number, the bot asks
three questions and from then on every collect run DMs them the new jobs that match. Settings:
`JOBAPPLY_WHATSAPP_BOT_ENABLED` (default true; false = the webhook answers but nothing is posted or replied)
and `JOBAPPLY_WHATSAPP_WEBHOOK_SECRET` (the shared secret below; empty = the webhook answers 503).

### How people join

The share link on the admin card is `https://wa.me/<bridge number digits>?text=start` (the digits come from
the bridge's `GET /health` `me`; the UI shows the number masked, only the link carries it). Opening it starts
a chat with the bridge's number with *start* prefilled. The conversation:

```
you:  start                      (also HI / HELLO; any other first text from an unknown number is ignored -
                                  the bridge is a real phone)
bot:  Hi! I'm the *JOB-APPLY* job alerts bot. I'll message you here when new jobs matching your career
      field and location show up.

      Replies here are automated. Send *STOP* at any time to pause the alerts, *HELP* for all commands.

      First, what's your name?
you:  Priya
bot:  Thanks, Priya! Which career fields do you want jobs for? Reply with the numbers (e.g. *1, 9*) or
      type the names.

      1. Cybersecurity
      2. Software Engineering
      3. DevOps / SRE / Infrastructure
      4. Data & AI
      5. Product Management
      6. Design
      7. QA / Test Engineering
      8. IT Support / Sysadmin / Network
      9. Cloud Engineering
      10. Project / Program Management
      11. Sales
      12. Marketing
      13. Finance & Accounting
      14. HR / People
      15. Customer Success & Support
      16. Internships (students)
you:  1, 9                       (numbers, comma or space separated; names work too: "cyber and cloud")
bot:  Got it: *Cybersecurity, Cloud Engineering*. Where do you want jobs? Reply with a city (e.g. *Pune*),
      *remote*, or *any*. You can combine them: *Bengaluru, remote*.
you:  Pune, remote
bot:  You're all set, Priya! ✅  Fields: ... Location: *Pune, remote* ... Commands: STATUS, FIELDS,
      LOCATION, STOP, HELP.
```

States (`subscribers.state`): `new` -> `asked_name` -> `asked_fields` -> `asked_location` -> `active`
(FIELDS / LOCATION from an active subscriber pass through `edit_fields` / `edit_location`). The list is
`roles.all_fields()` (15 fields) plus *Internships (students)*, stored as the pseudo-field `intern` in
`subscribers.fields` (= seniority intern). A location reply containing *remote* sets `remote_ok`; *any*
means anywhere.

Commands, case-insensitive, in any state: **START / HI / HELLO** (begin, or resume an unfinished sign-up),
**STOP** (pause: `active=false`, confirmed), **RESUME**, **FIELDS** (pick again), **LOCATION**, **STATUS**
(what they subscribed to), **HELP**. Other text from an active subscriber gets one short help line, at most
once per 10 minutes per number; more than 20 texts per number per hour are dropped silently. Group messages
are ignored except for the `!` commands below. Every message is short, plain WhatsApp text (`*bold*`), at
most one emoji.

### What they get

`bot.dispatch.notify_subscribers(session, new_job_ids)` runs at the end of every `collect.run_all` (after
the user alerts and the group feeds, in its own try/except) and from `jobapply bot dispatch` / the admin
card. Per active subscriber with a finished sign-up: the new jobs (or, without ids, the jobs first seen in
the last 24 h) that pass **the users' matcher** (`pipeline.filters.match`) with filters built from the row -
`role_selection = [field:*, ...]`, `seniority = ["intern"]` when internships were picked (internships only
when no field was picked), `locations = [location]` plus `remote`, no remote-only, `max_age_hours = 48` -
minus what `subscriber_posts` already holds. Newest first, batches of 8 jobs, at most **3 messages per
subscriber and run**; the rest goes out next run.

**Claim before send.** The batch's `subscriber_posts` rows (unique per subscriber and job) are committed
*before* the DM and deleted again when the send fails, so a job never reaches a number twice (not even from
two overlapping runs) and a failed send is retried next run. One message (the feed job block, no dashboard
link):

```
🆕 2 new jobs for you — 14:05 IST, 05 Nov

*SOC Analyst* — Acme Inc · Pune
🕒 posted 12 min ago (13:53 IST, 05 Nov) · via greenhouse
https://apply.example/1

*Cloud Engineer* — ...

Reply STOP to pause these alerts.
```

### Group commands

A WhatsApp group that has a **feed** (Group feeds card, `group_jid`) answers texts that start with `!`;
any other group message, and every message in a group without a feed, is ignored. One command per group
per minute (extras dropped silently); a reply never carries more than 8 jobs or ~3500 characters and goes
back to the same group.

| command | reply |
| --- | --- |
| `!jobs [field] [city] [Nh\|Nd]` | up to 8 matching jobs from the pool, newest first - default the feed's own filters and the last 24 h; the words resolve like the DM bot's (field label words or list numbers, `24h` / `3d`, the rest is the location), in any order: `!jobs cyber bengaluru 48h` |
| `!intern [city]` | internships only (seniority intern) inside the feed's filters |
| `!remote [field]` | remote jobs |
| `!latest` | the newest 5 jobs matching the feed, any age |
| `!help` | the list |
| `!feed status` | the feed's filters, state and counters |
| `!feed location <city>[, <city>]` / `any`, `!feed field <name\|number>`, `!feed hours <N>`, `!feed pause`, `!feed resume` | **admin only**: edit that group's feed in place (same `update_feed` as the admin card) |

Admin = the sender is the bridge's own number (`bridge_health` / `notify.bridge_me_digits`) or the
`whatsapp_number` of an admin dashboard user; anyone else gets *Sorry, only an admin can change this feed.*

### Admin card

`/admin` -> **Subscribers** (admin only, CSRF on every POST): the bot chip (on / disabled / secret unset),
the share link, the count of active subscribers per field, and the table (masked number `••••1234`, name,
fields, location, state, last message, posted count) with per-row **Suspend** / **Reactivate**
(`POST /admin/subscribers/<id>/suspend`, `/reactivate`) and **Delete** (`/remove`, also drops the
posted-job rows; the person can START again). **Send test post (dry run)** (`POST /admin/subscribers/preview`)
renders what the next dispatch would send per subscriber; nothing is sent or recorded.

### CLI

```
jobapply bot subscribers                       # id, on/off, masked number, name, state, fields, location
jobapply bot dispatch [--dry-run]              # post (or print) each subscriber's new matches
jobapply bot simulate --from 9198...@s.whatsapp.net "start"   # one inbound text through the engine
```

`simulate` prints the replies through a fake sender - nothing is ever sent - but the subscriber row changes
exactly as in a real chat, so use a test number (and `--from <group jid>` for `!` commands).

### The bridge webhook

The bridge (`~/whatsapp-headless`, Express + Baileys) POSTs every inbound text to JOB-APPLY:

- `src/config.js`: `webhookUrl` / `webhookSecret` from `WEBHOOK_URL` / `WEBHOOK_SECRET` in its `.env`
  (empty URL = off).
- `src/api.js`: next to the `wa.on('message')` SSE broadcast, `postWebhook(rec)` fires for every record that
  is inbound (`!fromMe`), not a history-sync replay (`meta.history`), not `status@broadcast` and has text:
  `POST WEBHOOK_URL` with JSON `{id, chat, from, phone, pushName, text, isGroup, ts}` and the header
  `X-Webhook-Secret: <secret>`, 5 s timeout, fire-and-forget (failures are one warning in the bridge log,
  nothing is retried). `phone` is the `...@s.whatsapp.net` jid when the bridge knows it (chats may show a
  `@lid` jid); the bot answers `phone` first, then `chat`.
- `.env` of the bridge: `WEBHOOK_URL=http://127.0.0.1:8010/api/whatsapp/inbound` and `WEBHOOK_SECRET=<hex>`
  (`openssl rand -hex 24`); the same value is `JOBAPPLY_WHATSAPP_WEBHOOK_SECRET` in `~/jobapply/.env`.
  Restart with `systemctl --user restart whatsapp-headless.service`, check `GET /api/health` says `open`.

`POST /api/whatsapp/inbound` (`web/routes/whatsapp.py`, no login, no CSRF) answers 503 while the secret is
unset, 401 when the header does not match, and otherwise `{"ok": true, "replies": n}` - a bad payload or a
bot error is logged, never raised back to the bridge. `bot.whatsapp.handle` runs in a worker thread.

**Carrying the bridge patch to its repo.** `~/whatsapp-headless` on the box is a plain copy, not a checkout;
the untouched originals are `src/api.js.bak-bot` and `src/config.js.bak-bot` and the unified diff is
`~/whatsapp-headless/patches/webhook.patch` (`diff -u` of the `.bak-bot` files against the patched ones).
In the bridge repo: `git apply patches/webhook.patch`, add `WEBHOOK_URL` / `WEBHOOK_SECRET` to its
`.env.example`, and document the payload above in its `API.md`.

## Tests never touch the live bridge

`tests/conftest.py` installs the log-only notifier before every test (the box's `.env` points at the live
bridge on the owner's phone); tests that need a transport build a `WhatsAppNotifier` on an
`httpx.MockTransport` or install a spy with `set_notifier`. The bot tests pass a fake `send` to
`bot.whatsapp.handle`, and the dispatch tests use the spy; the webhook tests post to the TestClient only.
