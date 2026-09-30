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

## The second standing authorization, granted

The page is published from `docs/`, so publishing means committing there — and
merging, if it is to happen without a person. That is a **second** standing
authorization to write to `main`, and the maintainer granted it on 2026-09-29.

It is bounded the same way the race report's is, and no wider:

| Bound | |
|---|---|
| Path | `docs/scoreboard.json`, and `docs/scoreboard.html` when regenerated. Nothing else, ever. |
| Check | a green `Tests` run on the PR head. Red CI leaves the PR open and is reported. |
| Deploy | `docs/**` is in `ignored_files`, so the merge does not rebuild the service. |

Everything outside those two paths stays ask-first, on the same terms as the race
report: never app code, never terraform, never another Routine's file, and never a
merge by any route other than a PR with a green check.

The decision was made deliberately rather than inherited, which matters because an
earlier draft of this file argued the other way — that the race report's
authorization was the one load-bearing risk and the bounds were worth keeping
scarce. What changed the answer: the blast radius here is two files the running
service never reads, behind a deploy filter, and the race report's far riskier
authorization has run without a bad merge since 2026-09-22.

The withheld alternative was a Routine that opens a PR and stops. It was rejected
because a daily metric nobody merges is a daily metric that is usually a day stale,
which defeats the point of publishing it.

## The deploy filter is live

`docs/**` has been in `ignored_files` since #220's apply on 2026-09-27, alongside
`operations/**`. Without it, a commit to `docs/` would fire a build and redeploy
the live booking service, and this Routine would do that every day it published.

**A merge to `main` applies terraform.** `cloudbuild.yaml` runs
`terraform apply -auto-approve` as its last build step, so an infrastructure change
is live once it merges and the build succeeds. There is no separate manual apply,
and an earlier version of this file said there was.

It is checkable, and was checked. `gcloud builds triggers describe` is denied to
the session service account, but Cloud Build writes to Cloud Logging and that
account holds `roles/logging.viewer`:

```
2026-09-27T02:11:17  Step #3: ~ ignored_files = [
2026-09-27T02:11:17  Step #3:     + "docs/**",
2026-09-27T02:11:59  Step #3: Apply complete! Resources: 0 added, 4 changed, 0 destroyed.
```

#220 merged at 02:06Z; that is the apply five minutes later. A routine build
reports `3 changed`; the fourth was this filter.

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
   The repository holds the schemas; the files there are empty by design. Each
   Routine writes one object per run under its own prefix, so this is a list and
   then a read of everything found:
   `gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<routine>/<YYYY-MM-DD>.json`.
2. Derive the three metrics per `operations/scoreboard.md`: outcome split as
   all-time and last-28-day totals, the automation streak as the combined
   consecutive count plus each Routine's own, cost from the newest `cost.jsonl` row.
   Record `null` with a reason for any source that does not exist. Do not infer,
   and do not substitute zero. Rows with a `backfill` object count toward the
   outcome split and are skipped by the streak (`operations/scoreboard.md` §2).
   `operations/ledger/derive_scoreboard.py` already derives and validates the
   outcome split; the streak walk is the part this Routine adds.
3. Write `docs/scoreboard.json`, commit it on a branch, open a PR, merge on green
   `Tests`. That is the standing authorization above, and it covers those two
   paths and nothing else.
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
| `docs/scoreboard.json` | the values the page reads | overwritten on change | **backfilled** (2026-09-30) |

The page is live against 23 mornings backfilled from the race reports, flagged
by a banner keyed off the `backfill` object in the JSON (`"sample": true` still
drives the sample banner, now unused). The banner stays while any published row is
a backfill row, which is correct: those outcomes were transcribed, not read. A Routine only ever writes the JSON; it does not
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

Two prerequisites are already met: the authorization is granted, and `docs/**` is
live in the deploy filter. What remains:

1. **The race report Routine must be writing its ledger first.** This Routine
   derives from those rows and has nothing to read until they exist. That is the
   prompt in `race-report.md`, pending a paste into its trigger.
2. Create the Routine, record its trigger ID in the table above, and change
   **Status** to deployed.
3. Note its cron in the 2026-11-01 DST change, which then covers two Routines
   rather than one.

Until step 1 produces rows, this Routine would publish a page of zeros and nulls,
which is worse than the sample data now showing — the sample is labelled, and
zeros would not be.
