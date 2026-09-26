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

`docs/**` is now in `ignored_files` in `terraform/main.tf`, alongside
`operations/**`. Without it, a commit to `docs/` fires a build and redeploys the
live booking service, so this Routine would redeploy production every day it
published.

**The edit is not the prerequisite; the apply is.** Nothing in CI runs
`terraform apply`, so merging the change does not alter the live trigger. And no
session can confirm it afterwards: the service account
(`teetime-artifact-reader`) gets `PERMISSION_DENIED` on
`gcloud builds triggers describe`.

An attempt to verify it indirectly produced no signal, which is worth recording
rather than repeating. Cloud Build posts no commit statuses to GitHub, so an
`operations/`-only commit (1a9d4de, a race report) and a commit that changes app
code (cabb678) both return zero statuses. The check cannot distinguish "filtered"
from "deployed", so it says nothing about whether `operations/**` is live either.

Confirm against the live trigger, by whoever can read it, before turning this
Routine on.

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

1. Read the ledgers **from GCS**, not from `operations/ledger/` in the checkout.
   The repository holds the schemas; the files there are empty by design. The
   objects are `gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<routine>.jsonl`.
2. Derive the three metrics per `operations/scoreboard.md`: outcome split as
   all-time and last-28-day totals, the automation streak as the combined
   consecutive count plus each Routine's own, cost from the newest `cost.jsonl` row.
   Record `null` with a reason for any source that does not exist. Do not infer,
   and do not substitute zero.
3. Write `docs/scoreboard.json`, commit it on a branch, open a PR, merge on green
   `Tests`.
4. Append this run's row to `scoreboard.jsonl`.
5. **Notify only on a change in a source metric, or on a failure.** A failure
   includes the append failing, and a source that previously worked having stopped
   — a silent gap is the one failure mode this Routine exists to prevent.

### It publishes every run, not only when something changed

An earlier draft had a "if nothing changed, append and stop without publishing"
branch. That branch can never be taken, and the reason is worth keeping.

The automation streak counts successful runs across **every** Routine, this one
included. So each successful scoreboard run increments the streak, which is one of
the three metrics it publishes. Something always changed. A no-change branch
gated on "did any metric move" would therefore never fire, and a branch that
excluded this Routine's own runs from the comparison would be comparing against a
number it is not publishing.

Publishing every run is the honest resolution, and it makes the ledger's own rule
hold: one row per scheduled day, and a gap means a missed run. The notification
still fires only on a change in a **source** metric, because the streak moving by
one every day is not news.

## The page

| File | Role | Churn | State |
|---|---|---|---|
| `docs/scoreboard.html` | the page: layout, styling, the charts | written once, reviewed once | **written** |
| `docs/scoreboard.json` | the values the page reads | overwritten on change | **sample data** |

The page is live against sample data, flagged on the page itself with a visible
banner keyed off `"sample": true` in the JSON. The first real run overwrites the
file and the banner disappears. A Routine only ever writes the JSON; it does not
regenerate the HTML.

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

## What the page renders, and why those forms

Chosen against `choosing-a-form`, and the colors were run through the palette
validator rather than eyeballed. Two results worth recording, because both
contradicted the obvious choice:

**Booked rate is a meter, not a two-colour bar.** It is a single ratio, and the
prescribed form for a single ratio is a meter on a same-ramp track. That also
avoids the problem below.

**Green-for-booked against red-for-missed fails.** The status pair `#0ca30c` /
`#d03b3b` measures CVD ΔE 4.1 (deutan) in both modes, against a floor of 8 — the
classic red/green failure. It would have shipped on instinct. The three-bucket
view, once #216 makes exact and fallback separable, uses categorical slots 1–3
(blue `#2a78d6`, orange `#eb6834`, aqua `#1baf7a`), which pass all-pairs in both
modes.

Light-mode aqua carries a contrast warning at 2.74:1, so the page ships direct
labels and a table view, which is the documented relief.

Exactly one hero figure, per the spec: the all-time booked rate. Everything else
is a stat tile. The streak history is one series, so it carries no legend.

## Deploying it

1. Apply the terraform, and confirm `ignored_files` on the live trigger reads
   `["operations/**", "docs/**"]`. **Not optional** — without it this Routine
   redeploys production daily. The repository edit is already made; the apply is
   not, and cannot be verified from a session.
2. Create the Routine, record its trigger ID above, and change **Status**.
3. Note the new cron in the 2026-11-01 DST change.

Until then the page shows sample data, flagged as such.
