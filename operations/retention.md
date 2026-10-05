# Retention

How long each kind of data is kept, why, and what enforces it. Issue #269.

Before #269 nothing set a period for anything. Two bounds existed by accident:
Cloud Logging's 30-day default, and Cloud SQL's 7 retained backups.

**The periods below are proposals.** They were set by the session that built the
mechanism, from what each store is used for, and they are the maintainer's to
change. Applying them deletes data (see Applying), so merge is the decision.

## Policy

| Store | Holds | Kept | Enforced by |
|---|---|---|---|
| `bookings` (Cloud SQL) | requests and outcomes | finished rows until 400 days after the date played; live rows never | `purge_expired`, `retention_booking_days` |
| `sessions` (Cloud SQL) | conversation state | idle rows 90 days after the last message; a mid-conversation row never | `retention_session_days` |
| `tee_sheet_grids` (Cloud SQL) | observer slot-grid readings | 90 days after the reading | `retention_tee_sheet_grid_days` |
| `walden_credentials` (Cloud SQL) | a member's stored login | until `/forget` or leaving the group (existing); 90 days after Walden rejected it with no replacement | `retention_invalid_login_days` |
| `member_pseudonyms` (Cloud SQL) | label per member | **indefinitely** | nothing deletes it, by design |
| `walden/` in the debug-artifacts bucket | failure captures, race artifacts, observer snapshots | 90 days, then a 7-day noncurrent version, then 7 days of soft delete | bucket lifecycle rules, `debug_artifact_retention_days` |
| `operations/` in the same bucket | Routine ledgers | **indefinitely** | no rule matches the prefix |
| `billing_export` (BigQuery) | Cloud Billing export | **indefinitely** | none; it cannot be re-run for past days |
| Cloud Logging | service and job logs | 30 days (`_Default`); 400 days locked (`_Required`) | Cloud Logging defaults, unchanged |
| Cloud SQL backups | automated backups, point-in-time logs | 7 backups | Cloud SQL setting, unchanged |
| `operations/` in git | race reports, scoreboard | indefinitely | git; the repository is public |

The SQL periods are settings in `app/config.py`, read in one place: the purge,
`app/services/retention.py`. It runs daily at 03:15 CT (Cloud Scheduler
`teetime-purge-expired` -> `POST /jobs/purge-expired`), three hours before the
race, so it cannot hold a lock when the racer claims its bookings.

## Why these periods

- **Finished bookings, 400 days after play.** The scoreboard and cost metrics
  read GCS ledgers, not this table, so history here serves only the member
  ("what did you book?") and diagnosis. A little over a year lets a season be
  compared with the last one. Counted from the date played, so a request made
  months ahead is not old until its date is. Only SUCCESS, FAILED and CANCELLED
  are ever deleted; PENDING, SCHEDULED and IN_PROGRESS belong to the racer and
  the startup reconciler however old they look.
- **Idle sessions, 90 days.** An idle session carries nothing: the next message
  creates a new one.
- **Grid readings, 90 days.** The booking conversation reads only the latest
  reading of an upcoming date. Older ones serve a post-mortem, and a morning is
  diagnosed within days.
- **Rejected logins, 90 days.** A rejected login is not tried again (#244), so it
  does nothing but sit there as an encrypted copy of a member's password. An
  *unused but working* login is deliberately not purged: leaving the group
  already deletes it, and whether a quiet member still wants it is theirs to say.
- **`walden/` captures, 90 days.** Copies of the club's tee sheet can show
  members' names (#143). Race reports are written within a day, and the
  observer's structural findings are recorded in the reports and in
  `tee_sheet_grids`, so the raw snapshots stop being the only evidence.
  *Decision for the maintainer:* the observer snapshots (`walden/observer/`,
  ~60 MB over three weeks) are the stated ground truth for what a real D+7 sheet
  looks like. If they should outlive the failure captures, they need their own
  longer rule, which means splitting the one `walden/` rule by prefix.
- **Pseudonyms and `operations/`, indefinitely.** A label is never reused, or an
  old public report would come to mean someone else. The ledgers are
  measurements with no member content, and their append-only grant means nothing
  can remove them anyway.
- **Billing export, indefinitely.** Small, and unrecoverable once lost.

## Applying

Merging applies the terraform (`cloudbuild.yaml`), and the first purge runs that
night. Both delete:

- Every `walden/` object already older than 90 days. Checked 2026-10-05: 36
  objects predate 2026-07-07, the oldest from 2026-02-08, all failure captures
  (`fast_chain_failed_*`, `player_count_selection_failed`,
  `booking_verification_failed`); none is a race artifact or observer snapshot.
  They sit in the 7-day soft-delete window first, so an undo is
  `gcloud storage restore`, for a week.
- Whatever the SQL rules match. Run `?dry_run=true` first and read the counts:

```bash
curl -s -X POST "$SERVICE_URL/jobs/purge-expired?dry_run=true" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

## What is not established

- Whether anyone relies on a booking row older than 400 days. Nothing in the repo
  reads one; that is a search of this code, not of a person's habits.
- Cloud Logging retention was read from the bucket list (`_Default` 30 days) and
  not from the sink configuration. A log-based export would change the answer;
  none was looked for.
- Secret Manager version retention. Old versions of a secret may persist after a
  rotation; this was not checked, and no rule here covers it.
- Cloud SQL point-in-time log retention, and the exact age of the oldest backup.
  Only `retainedBackups=7` was read. A deleted row can persist in a backup for
  that long, so "deleted" in the table above means deleted from the live
  database.
- Telegram's own copy of a conversation, and Gemini's handling of message text,
  are outside this system's control. `docs/privacy.md` names Gemini as a
  recipient.
- The purge has run only against an in-memory SQLite database in tests. It has
  not run against Postgres. The first run is the dry run above.
