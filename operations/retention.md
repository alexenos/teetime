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
| `bookings` (Cloud SQL) | requests and outcomes | **indefinitely**, to study booking trends over years | nothing deletes it, by decision of the maintainer |
| `sessions` (Cloud SQL) | conversation state | idle rows 90 days after the last message; a mid-conversation row never | `retention_session_days` |
| `tee_sheet_grids` (Cloud SQL) | observer slot-grid readings | 90 days after the reading | `retention_tee_sheet_grid_days` |
| `walden_credentials` (Cloud SQL) | a member's stored login | until `/forget` or leaving the group (existing); 90 days after Walden rejected it with no replacement | `retention_invalid_login_days` |
| `member_pseudonyms` (Cloud SQL) | label per member | **indefinitely** | nothing deletes it, by design |
| `walden/` in the debug-artifacts bucket | failure captures, race artifacts, observer snapshots | 90 days, then 7 days as a noncurrent version, then 7 days of soft delete; an overwritten object's old version goes at 90 days too, not sooner | bucket lifecycle rules, `debug_artifact_retention_days` |
| `operations/` in the same bucket | Routine ledgers | **indefinitely** | no rule matches the prefix |
| `billing_export` (BigQuery) | Cloud Billing export | **indefinitely** | none; it cannot be re-run for past days |
| Cloud Logging | service and job logs | 30 days (`_Default`); 400 days locked (`_Required`) | Cloud Logging defaults, unchanged |
| Cloud SQL backups | automated backups, point-in-time logs | 7 daily backups and 7 days of transaction logs | Cloud SQL settings, unchanged |
| `operations/` in git | race reports, scoreboard | indefinitely | git; the repository is public |

Read from the live instance on 2026-10-09: `retainedBackups: 7` (7 daily backups listed, 2026-10-02 to 2026-10-08) and `transactionLogRetentionDays: 7`. So a row deleted from the live database can remain in a backup or the logs for about a week, and "deleted" above means deleted from the live database.

The SQL periods are settings in `app/config.py`, read in one place: the purge,
`app/services/retention.py`. It runs daily at 03:15 CT (Cloud Scheduler
`teetime-purge-expired` -> `POST /jobs/purge-expired`), three hours before the
race, so it cannot hold a lock when the racer claims its bookings.

## Why these periods

- **Bookings, indefinitely.** Kept on purpose, to look for trends in how members
  book over years (maintainer, 2026-10-06). The row holds a member's requester ID
  (their Telegram ID), so this is the one table where keeping everything leaves
  a member's history on file after they leave; `docs/privacy.md` says so, and
  removal is by asking. Anonymising a leaver's rows instead of deleting them
  would keep the trend data without the identity, and is not built.
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

- Cloud Logging retention was read from the bucket list (`_Default` 30 days) and
  not from the sink configuration. A log-based export would change the answer;
  none was looked for.
- Secret Manager version retention. Old versions of a secret may persist after a
  rotation; this was not checked, and no rule here covers it.
- Telegram's own copy of a conversation, and Gemini's handling of message text,
  are outside this system's control. `docs/privacy.md` names Gemini as a
  recipient.
- The purge has run only against an in-memory SQLite database in tests. It has
  not run against Postgres. The first run is the dry run above.
