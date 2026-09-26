# Routine: cost

**Status: proposed. Not deployed, and cannot be until a billing export exists.**

| | |
|---|---|
| **Trigger** | monthly, proposed — `0 14 1 * *` UTC (08:00 CT on the 1st), reporting the month just ended |
| **Authorization** | queries BigQuery, appends to GCS. Commits nothing. |
| **Emits** | one row in `operations/ledger/cost.jsonl` |
| **Owns** | the cost metric |

Separate from the scoreboard Routine on the same rule as every other source: a
Routine owns the metrics it measures, and the scoreboard only derives. Keeping it
separate also keeps BigQuery credentials out of the scoreboard Routine, which
otherwise needs no access to anything but the ledgers.

## What it is blocked on

**Cloud Billing export to BigQuery is not enabled.** Verified: no service account
in `terraform/` holds a billing role — the grants are storage, logging, run,
cloudsql, secretmanager, artifactregistry, cloudscheduler and serviceusage — and
there is no BigQuery dataset or billing configuration anywhere in the repository.

The export is configured on the **billing account**, not the project. Terraform
here manages the project, so this repository cannot enable it. It needs someone
holding `roles/billing.admin` on the billing account to turn on "Detailed usage
cost" export, which creates a dataset named
`gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`.

Once that exists, this repository can grant the access:

- `roles/bigquery.jobUser` on the project, to run a query
- `roles/bigquery.dataViewer` on the export dataset, to read it

Both are ordinary terraform. `bq` is already installed in the session environment,
so the query needs no new tooling.

## What it cannot cover

**Agent and token spend is not in GCP.** It is Anthropic spend, and no
programmatic source for it is available to these sessions. Two honest options:

1. Report GCP only, and label the figure `scope: gcp_only` on the page.
2. Enter a monthly figure by hand, and label it `scope: gcp_plus_agent` with
   `usd_agent` recorded as manually entered.

Not decided. Until it is, option 1 is what the schema assumes.

**A cost number whose scope is unstated is worse than no number**, because it
invites $/booking comparisons against a denominator that does not match.

## What a run does

1. Query the billing export for the month just ended, grouped by service.
2. Read `exact + fallback` for that month from `race-report.jsonl` — the booking
   count, and the divisor for $/booking.
3. Append one row to `cost.jsonl`, with `scope` stating what the figure covers and
   `usd_agent` as `null` unless a figure was supplied.
4. If the query fails or returns no rows, append a row with `ok: false` and the
   reason. A month with no row is indistinguishable from a month that was never
   checked.

## Shares the DST defect

A cron pinned to UTC drifts against CT twice a year. At monthly granularity the
consequence is an hour, not a missed run, so this one is cosmetic rather than
load-bearing — unlike the race report, where 05:40 CT lands before the race it
reports on. Recorded so the set of crons needing the 2026-11-01 change is complete.

## Deploying it

1. Enable the billing export (outside this repository).
2. Add the two IAM grants in `terraform/`.
3. Create the Routine, record its trigger ID here, and change **Status**.
4. Decide the agent-spend question above and record the decision here.

Until step 1 happens, cost is `null` on every scoreboard row and the page shows it
as unavailable rather than as zero.
