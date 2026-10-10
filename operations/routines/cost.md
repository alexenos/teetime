# Routine: cost

**Status: proposed, no longer blocked. Not deployed.** The billing export is on and the query has run against real rows (2026-10-09); what remains is creating the Routine (Deploying it, step 4).

| | |
|---|---|
| **Trigger** | monthly, proposed — `0 11 5 * *` UTC (05:00 CST on the 5th, the maintainer's choice 2026-10-09), reporting the month just ended |
| **Authorization** | queries BigQuery, writes one new object to GCS. Commits nothing. |
| **Emits** | one object per run at `operations/cost/<YYYY-MM>.json` in GCS |
| **Owns** | the cost metric |

Separate from the scoreboard Routine on the same rule as every other source: a
Routine owns the metrics it measures, and the scoreboard only derives. Keeping it
separate also keeps BigQuery credentials out of the scoreboard Routine, which
otherwise needs no access to anything but the ledgers.

Scope, prerequisites and the reasoning for BigQuery are in **#227**. Whether
Anthropic spend can be measured at all is **#228**. This file records what the
Routine does; those issues record what has to exist first.

## What it was blocked on

**Resolved 2026-10-04: the export is enabled**, and by 2026-10-09 it held September in full. What follows is the history of why it took a Console step. Verified 2026-10-03: the
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
billing account may pay for other projects, and on `invoice.month`. Run against real rows on 2026-10-09: September
net $37.17, gross $42.39, matching the Console's $37.17 ("includes -$2.92 in
savings, $2.30 tax"). The $2.30 is the `Invoice` service, which is tax, so the
figure is net of credits and includes tax. The export holds only $5.64 for August
against the Console's ~$71: the backfill starts the previous month but is not
complete for it, so **August is not a usable prior month**.

## What it cannot cover

**Agent and token spend is not in GCP**, and whether it can be measured at all is
not established — that is #228, not a settled absence. Scope is `gcp_only` until
that issue resolves, labelled as such on the row and on the page.

**A cost number whose scope is unstated is worse than no number**, because it
invites $/booking comparisons against a denominator that does not match.

## What a run does

1. Query the billing export for the month just ended, grouped by service.
2. Write one row to GCS at `operations/cost/<YYYY-MM>.json`, with `scope` stating
   what the figure covers and `usd_agent` as `null` unless a figure was supplied.
   `scripts/cost_row.py` does steps 1 and 2's row: `python scripts/cost_row.py --out
   cost-row.json`, then `gcloud storage cp --no-clobber`. $/booking is not stored: the
   scoreboard derives it from `race-report` rows for the same month.
3. If the query fails or returns no rows, append a row with `ok: false` and the
   reason. A month with no row is indistinguishable from a month that was never
   checked.

## Shares the DST defect

A cron pinned to UTC drifts against CT twice a year: `0 11 5 * *` is 06:00 CDT and
05:00 CST. At monthly granularity the consequence is an hour, and either hour is
before the scoreboard Routine's 07:30 CT (06:30 CST until its own cron moves), so
this one is cosmetic rather than load-bearing — unlike the race report, where 05:40
CT lands before the race it reports on. Recorded so the set of crons needing the
2026-11-01 review is complete, even where the answer is "leave it".

## When to run it

**The 5th, chosen 2026-10-09 by the maintainer.** The reasoning is a hypothesis,
not a measurement: export rows can arrive late, `invoice.month` attributes late
adjustments (credits, tax) to the month they bill in, and the invoice settles a
few days after month end, so a run on the 1st may read a month that is not final.
A delay costs nothing here, since the figure is a monthly headline.

**Not verified:** how much September's figure moved between the 1st and the 5th.
It was read on 2026-10-09 and matched the Console, which says only that it had
settled by the 9th. Run `cost.sql` for October on 2026-11-01 as well as on the
5th and compare; if the figure never moves, the trigger could go earlier.

This Routine fires before the scoreboard Routine on the same morning, in every
month and whichever way DST falls: `0 11 5 * *` is 05:00 CST or 06:00 CDT, and the
scoreboard is at 06:30 CST or 07:30 CDT. The ordering is deliberate (maintainer,
2026-10-09), and this cron needs no change at either DST boundary.

Separate defect, not this Routine's: until the scoreboard cron moves from `30 12`
to `30 13` UTC on 2026-11-01 (`operations/routines/scoreboard.md`, Shares the DST
defect), its own hour gate stops it before 07:00 CT. On 2026-11-05 that would
skip the scoreboard run whatever the cost row says.

## Deploying it

1. ~~Merge `terraform/cost.tf` (#227) and confirm the apply.~~ Done 2026-10-04.
2. ~~Enable the billing export in the Console.~~ Done 2026-10-04.
3. ~~Run `cost.sql` for September and check it against the Console.~~ Done
   2026-10-09: $37.17 both. The seed row is written by `scripts/cost_row.py --seed`
   to `operations/cost/2026-09.json`; it carries a `backfill` object, so it is
   outside the streak.
4. Create the Routine, record its trigger ID here, and change **Status**.
5. Resolve #228 and record the scope decision here.

The scoreboard shows the seeded September figure once the scoreboard Routine's
prompt reads `operations/cost` (see `operations/routines/scoreboard.md`, Step 2).
The cost Routine's first scheduled run is 2026-11-05 for October; until then the
seed is the newest row, and `derive_scoreboard.py` expects that run on that date
and counts its absence as a break in the streak.
