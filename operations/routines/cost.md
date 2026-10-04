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

Scope, prerequisites and the reasoning for BigQuery are in **#227**. Whether
Anthropic spend can be measured at all is **#228**. This file records what the
Routine does; those issues record what has to exist first.

## What it is blocked on

**Cloud Billing export to BigQuery is not enabled.** Verified 2026-10-03: the
project billing link is enabled, the BigQuery API was not enabled on the project,
so no export dataset could exist.

The export is configured on the **billing account**, not the project, and has no
API or terraform resource. It needs someone holding `roles/billing.admin` on the
billing account — the maintainer does — to turn it on once in the Console.

Everything else is in `terraform/cost.tf` (#227):

- the `bigquery` API
- the dataset `billing_export` (US), with `prevent_destroy`. A first export to a
  multi-region dataset backfills from the start of the previous month, up to five
  days to complete; nothing older is recoverable, and a lost dataset is lost
  history. A single-region dataset would get no backfill, which is why it is US
- `roles/bigquery.jobUser` on the project for `teetime-artifact-reader`, to run
  a query
- `roles/bigquery.dataViewer` on `billing_export` only, to read it

Once that has applied, the Console step is:

> Billing → Billing export → BigQuery export → **Detailed usage cost** → Edit
> settings → project `gen-lang-client-0822973627`, dataset `billing_export`

"Detailed" rather than "Standard" because it carries per-resource attribution,
which is what makes the Cloud SQL question (#41, #168) answerable from data. It
creates one table, `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`.

The query is `cost.sql`, beside this file. It filters on `project.id`, because the
billing account may pay for other projects, and on `invoice.month`. It has not
been run against real rows.

## What it cannot cover

**Agent and token spend is not in GCP**, and whether it can be measured at all is
not established — that is #228, not a settled absence. Scope is `gcp_only` until
that issue resolves, labelled as such on the row and on the page.

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

A cron pinned to UTC drifts against CT twice a year: `0 14 1 * *` is 09:00 CDT and
08:00 CST. At monthly granularity the consequence is an hour, not a missed run, so
this one is cosmetic rather than load-bearing — unlike the race report, where 05:40
CT lands before the race it reports on. Recorded so the set of crons needing the
2026-11-01 review is complete, even where the answer is "leave it".

## When to run it

**Not verified:** Google documents that export rows can arrive with a delay, and
`invoice.month` attributes late adjustments to the month they bill in. A run at
08:00 CT on the 1st may read a month whose last day is incomplete. Check this
against the first real month before choosing the trigger: run `cost.sql` for the
same month on the 1st and again on the 5th, and compare. If they differ, move the
trigger to the 5th.

## Deploying it

1. Merge `terraform/cost.tf` (#227) and confirm the apply in the build log.
2. Enable the billing export in the Console, as above (outside this repository).
3. Once the initial backfill has finished (up to five days), run `cost.sql` by
   hand for the previous month and check it against the Console's figure for it.
4. Create the Routine, record its trigger ID here, and change **Status**.
5. Resolve #228 and record the scope decision here.

Until step 1 happens, cost is `null` on every scoreboard row and the page shows it
as unavailable rather than as zero.
