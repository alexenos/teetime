# The scoreboard

Three metrics. One measures member outcomes, one measures the automation, one
measures cost.

Each metric names the field it is computed from. On 2026-09-15 a valid,
zero-row log query was read as "no booking ran"; a metric without a stated
source cannot be checked against that failure mode.

---

## 1. Outcome split

Each booking request falls into exactly one bucket. Counted per request, not
per morning. Rolling 4 race mornings.

| Bucket | Definition |
|---|---|
| **Exact** | reserved the tee time the member agreed to |
| **Fallback** | reserved a tee time other than that one |
| **Miss** | no reservation |

**Source:** `RESERVATION_CHECK`. `phase=complete, success=True` does not
establish a reservation. This distinction is recorded in the `race-report`
skill and is the most likely element of this definition to regress.

**Condition for Exact:** the member's request must have been resolved to a
bookable slot and confirmed with the member before the race was scheduled, not
selected from a ±32-minute window at race time.

That confirmation step does not exist; it is #216. Until it ships, Exact and
Fallback cannot be distinguished, because a request for "8am" against a sheet
with no 08:00 slot has no reference value. Until then, report the split with
that limitation stated, or report Miss against not-Miss.

**Rationale for per-request counting:** 2026-09-15 was two requests, both
Exact. 2026-09-17 was three requests, all Miss. Per-morning counting loses
both figures.

## 2. Automation streak

Consecutive clean runs of the race report Routine.

The race report is the only automation this metric covers, because it is the
only Routine that runs on a schedule and acts without being asked. `ship-pr` and
the PR check-ins are maintainer-invoked and are not counted here.

**Clean** means the run's output required no correction before it could be
acted on. It is independent of the outcome the run reported: a correctly
diagnosed loss is clean, and so is a correctly reported "no booking was
scheduled".

The Routine now commits and merges its own report
(`operations/routines/race-report.md`), so an incorrect report reaches `main`.
A report that merged and then required a correction commit is not clean.

Two recorded cases, both predating that authorization — no auto-merged report
has yet required a correction:

| Date | Why not clean |
|---|---|
| 2026-09-15 | Reported no booking on a morning when two bookings succeeded. The log query was scoped to the Cloud Run service and excluded the jobs. Identified by maintainer pushback; fixed in #206. |
| 2026-09-18 | The report's headline finding, a "112ms escape" against `_RESERVE_TIMEOUT_S`, was wrong about which timeout applies to a burst fire. Retracted by #219 the following day. |

**Source:** `operations/ledger/runs.jsonl`, counted back from the newest row to
the first `clean: false`. Do not store the streak.

The run cannot reliably score itself — see the streak subsection under **How it
would get updated**, which proposes deriving `clean` from git history instead and
using a row only for the runs that produce no report.

## 3. Cost

Total spend per month, and $/booking.

$/booking is the input to the Cloud SQL retention decision open since #41 and
#168 (~$9.50/month). Include agent and token spend, not GCP alone.

**Source:** not available. Requires billing export access; the race report
service account is scoped to storage and logging, which is the same limitation
that left the memory-versus-CPU question unresolved on 2026-09-17.

---

## Excluded

**Commit count, PR count, lines changed.** These measure activity rather than
outcome, and are directly gameable by automation that generates its own work.

**Malformed response rate** and **fix latency** were specified and then cut, to
keep the scoreboard to what is worth maintaining at current volume. Neither was
instrumented. The incidents that motivated the first — a member sent a Selenium
stack trace, a member sent a message addressed to someone else, and a member
told a result would follow who received nothing, all on 2026-09-17 — remain
recorded in `operations/race-reports/2026-09-17.md`. They are no longer tracked
as a rate.

**Human-touch rate** — the proportion of merged PRs where the maintainer edited
code rather than only approving — was also cut. It measured the project's
original premise most directly, and it is the one excluded metric whose input
cannot be reconstructed after the fact: whether a given merged PR was edited by
hand is not recoverable from the repository later. Nothing is accumulating it.

## Computation

The scoreboard is a rollup over ledger rows and git metadata. It does not parse
the prose of the reports: two sessions can read the same paragraph and score it
differently. Reading git history *about* a report file — whether a later commit
modified it — is deterministic and is fair game; see the streak section below.

The reports in `operations/race-reports/` are unchanged by this. They hold the
analysis; the ledger and git hold the values used for computation. See
`operations/ledger/README.md`.

## Three levels, which are easy to conflate

| | Answers | Shape | Where |
|---|---|---|---|
| **Events** | what happened | one row per occurrence | `ledger/mornings.jsonl`, `ledger/runs.jsonl` |
| **Snapshots** | what the metrics read, and when | one row per day | `ledger/snapshots.jsonl` |
| **This document** | what the metrics mean | definitions | here |
| **The current view** | how we are doing now | rendered | nowhere yet; see below |

The values are not in this document and should not be. It holds definitions,
which are hand-written and reviewed; the values are computed, and a Routine
editing a hand-written file invites a conflict between the two.

**Snapshots are not a cache over events.** Each row records what the metrics read
on a date, under the definitions in force on that date, and carries a
`definitions` version saying which. Recomputing history under a later definition
produces numbers that were never true — #216 will change what Exact means, and a
recomputed trend would show a discontinuity that is not a change in performance.
Some values cannot be recomputed at all: month-to-date cost is not reconstructable
later, and agent and token spend is not queryable historically. See
`operations/ledger/README.md`.

## How it would get updated

Nothing updates it today. There is no writer, no rollup, and no rendered
current value: a grep for `operations/ledger` or `scoreboard` across `app/`,
`scripts/`, the workflows and terraform returns nothing. The sections above are
a specification.

The plan is one snapshot per day, from a second Routine at 07:30 CT that appends
a row, compares it against the previous one, and notifies only when something
moved. It commits nothing. Specified in
`operations/routines/scoreboard.md`, which is not deployed.

The three metrics are not equally far from working, and it is worth not treating
them as one task.

### Outcome split — needs a writer, and #216

The source is already read every morning. The race report Routine reads
`RESERVATION_CHECK` to reach its verdict, so the row is a byproduct of work that
is happening anyway; appending it is one GCS write at the end of Step 5. That
write is outside the Routine's authorized repository path and does not widen it.

What it cannot do until #216 ships is distinguish Exact from Fallback, for the
reason given above. Until then the row can carry `outcome` as `miss` or
`not_miss` honestly, and no more.

### Automation streak — the run cannot score itself

A run that misdiagnoses the morning believes it did fine. That is precisely what
2026-09-15 was: the session reported "no booking" and had no idea it was wrong.
So `clean` written by the run that is being scored is not a measurement.

Two ways out, and the second is better:

1. Write `clean: true` provisionally and amend later, per the amendment rule in
   `operations/ledger/README.md`. Requires someone to remember to amend.
2. **Derive it from git instead.** A report is clean if no later commit modified
   its file and no later PR retracted its findings. The first half is
   mechanically computable — `git log --follow` on
   `operations/race-reports/<date>.md` — and needs no writer and no row at all.
   It is also retroactive, so it works on all 23 existing reports.

Under (2) the streak stops needing `runs.jsonl` for reports. It still needs a row
for the runs that produce no report, since a morning with nothing scheduled is a
clean run with no file to inspect.

### Cost — no source exists at all

This one is not a code problem. The project has no billing export and no
BigQuery dataset, and no service account in `terraform/` holds a billing role;
the roles granted are storage, logging, run, cloudsql, secretmanager,
artifactregistry, scheduler and serviceusage. Agent and token spend is not in
GCP at all.

Cost therefore needs new GCP setup — a billing export and an access grant —
before any amount of code can read it. It is the only metric on this scoreboard
that cannot be computed from data the project already has.

### Where the current value appears

`scripts/scoreboard.py`, not yet written: reads `snapshots.jsonl`, prints the
newest row, the delta against the previous one, and a trend over any window
asked for. On demand, so a reader gets the current view without a scheduled job
rendering it into a file.

The snapshot Routine's notification covers the daily question — did anything
move — so the script is for looking at a trend rather than for finding out
whether to look.

**Not a committed rendered file.** That would need the snapshot Routine to hold a
standing authorization to write to the repository. The race report's authorization
is the one load-bearing risk in this setup, and a metrics job is a poor reason to
add a second.
