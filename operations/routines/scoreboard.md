# Routine: scoreboard

**Status: proposed. Not deployed.** No trigger exists. This file is the
specification; deploying it is the steps in the last section.

| | |
|---|---|
| **Trigger** | daily, `30 12 * * *` UTC (07:30 CT), proposed |
| **Authorization** | reads every ledger, writes `docs/scoreboard.json`, appends to its own ledger. **Requires a repository write — see below.** |
| **Emits** | `docs/scoreboard.json`, and one row in `operations/ledger/scoreboard.jsonl` |
| **Owns** | the derived metrics; no source data of its own |

It reads the other Routines' ledgers and derives. It never writes to them.

## It needs a repository write, and that has a prerequisite

The page is published from `docs/`, so publishing means committing there. That is
a second standing authorization to write to `main`, and it is bounded the same way
the race report's is: two paths (`docs/scoreboard.json` and, when regenerated,
`docs/scoreboard.html`), a green `Tests` check, and nothing else.

**`docs/` is not in the deploy filter.** The Cloud Build trigger sets
`ignored_files = ["operations/**"]` (`terraform/main.tf`). `docs/**` is absent, so
a commit to `docs/` fires a build and redeploys the live booking service. As
written, this Routine would redeploy production every day it publishes.

**Prerequisite: add `docs/**` to `ignored_files`.** One line of terraform, and it
has to be applied to the live trigger before this Routine is deployed. Until then
the Routine must not be turned on.

## Why it is separate from the race report

It would be cheaper to append this to the race report Routine, which already runs
daily. Three things argue against it.

**The race report's failure paths all stop early.** Wrong hour, no booking
scheduled, environment not ready — every one is *push, then stop*. A scoreboard
update at the end of that prompt is skipped on exactly the mornings something went
wrong, which is where a gap is least acceptable.

**It reads every ledger, not just the morning's.** Its input is all the Routines,
including its own history and the cost Routine's monthly rows.

**It needs today's report already merged.** Reports have merged at 06:48 and 06:51
CT, eight and eleven minutes after their run began. 07:30 CT clears that with
margin, and nothing here is time-critical — the morning's push notification went
out an hour earlier.

## What a run does

1. Read every `*.jsonl` in `operations/ledger/`.
2. Derive the three metrics per `operations/scoreboard.md`: outcome split as
   all-time and last-28-day totals, the automation streak as the combined
   consecutive count plus each Routine's own, cost from the newest `cost.jsonl` row.
   Record `null` with a reason for any
   source that does not exist. Do not infer, and do not substitute zero.
3. Compare against the newest row in `scoreboard.jsonl`.
4. **If nothing changed:** append the row and stop. No commit, no notification. The
   row is the record that the check happened.
5. **If anything changed:** write `docs/scoreboard.json`, commit it on a branch,
   open a PR, merge on green `Tests`, then append the row.
6. **Notify only on a change, or on a failure.** A failure includes the append
   failing and a source that previously worked having stopped — a silent gap is the
   one failure mode this Routine exists to prevent.

Step 4 is why the page is rebuilt "whenever a metric changes" rather than daily:
most days nothing moves, and a commit that changes no value is noise.

## The page

| File | Role | Churn |
|---|---|---|
| `docs/scoreboard.html` | the page: layout, styling, the trend charts | written once, reviewed once |
| `docs/scoreboard.json` | current values, deltas, and the trend series | overwritten on change |

Written as HTML rather than markdown because markdown tops out at tables: no
sparklines, no trend charts, no layout. A raw `.html` file with no Jekyll front
matter is copied through `docs/` untouched, so the minima theme does not wrap it.

Splitting the data from the page keeps each update to one small JSON diff instead
of a re-rendered page, so what changed is visible in the diff.

**The page is public.** Counts, rates and spend are fine; member identifiers are
not. See `operations/scoreboard.md`.

## Shares the DST defect

`30 12 * * *` UTC is 07:30 CT during CDT only. After 2026-11-01 it fires at 06:30
CT — during the race, and before the report it depends on has merged. The
correction is `30 13 * * *`, on the same date the race report's `40 11` becomes
`40 12`. Deploying this adds a second cron to change, so change them together.

## Deploying it

1. Add `docs/**` to `ignored_files` in `terraform/main.tf` and apply it. **Not
   optional** — without it this Routine redeploys production daily.
2. Write `docs/scoreboard.html`.
3. Create the Routine, record its trigger ID above, and change **Status**.
4. Note the new cron in the 2026-11-01 DST change.

Until then the scoreboard has definitions and no values.
