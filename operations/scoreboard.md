# The scoreboard

Three metrics: one for how members are served, one for how the automation is
holding up, one for what it costs.

Published as a page at `https://alexenos.github.io/teetime/scoreboard.html`,
rebuilt by the scoreboard Routine whenever a metric changes. This document holds
the definitions; it never holds the values.

Each metric names the field it is computed from. On 2026-09-15 a valid, zero-row
log query was read as "no booking ran"; a metric without a stated source cannot be
checked against that failure mode.

---

## 1. Outcome split

Each booking request falls into exactly one bucket. Counted per request, not per
morning.

| Bucket | Definition |
|---|---|
| **Exact** | reserved the tee time the member agreed to |
| **Fallback** | reserved a tee time other than that one |
| **Miss** | no reservation |

Two figures, both totals rather than averages:

| Figure | Window |
|---|---|
| All time | every row in `race-report.jsonl` |
| Last 4 weeks | rows dated within 28 days of the run |

**Trend:** last 4 weeks against the preceding 4 weeks.

**Source:** `race-report.jsonl`, the `outcome` object. Each row is one morning's
counts, written by the race report Routine from `RESERVATION_CHECK`.
`phase=complete, success=True` does not establish a reservation — a distinction
recorded in the `race-report` skill and the most likely element of this definition
to regress.

**Decision, 2026-10-03: Exact and Fallback are published for every morning.**
The maintainer judged the mechanical scoring of the older mornings good enough,
and dropped the page's disclaimer. The rest of this section is the stricter
reading that was set aside; `confirmed_slots` is still recorded on every row, so
it can be recovered.

**Exact and Fallback are distinguishable only for agreed requests.** Separating
them requires the member's request to have been resolved to a bookable slot and
confirmed before the race, rather than selected from a ±32-minute window at race
time. #216 adds that step where the club's sheet for the date has been read: the
member is offered the real tee times nearest their request, picks one, and the
race logs `agreed=sheet` for it. A request for "8am" against a sheet with no 08:00
slot has no reference value, so a request logged `agreed=unchecked` (no sheet on
file) or `agreed=none` (booked before #216) is still only Miss against not-Miss,
and the page must say so rather than showing an Exact count that means something
weaker than it reads.

**Miss against not-Miss is a presentation bucket, not a ledger value.** The row's
`outcome` is always one of `exact`, `fallback` or `miss`, scored mechanically from
booked against requested, so the totals always roll up and a booked request is
always counted as booked. Collapsing Exact and Fallback into not-Miss is something
the page does when `confirmed_slots` is false. A ledger that omitted the
distinction instead would lose the booking, not just the label.

Every row carries `confirmed_slots` for this reason. The page must not draw a
trend across a change in that field without marking it: the step would be a change
in definition, not in performance.

**Per request, not per morning.** 2026-09-15 was two requests, both booked.
2026-09-17 was three requests, all Miss. Per-morning counting loses both figures.

## 2. Automation streak

Consecutive successful runs across the Routines, counted back from the newest row
to the last failure.

| Figure | |
|---|---|
| Headline | the combined streak: consecutive successful runs, all Routines interleaved by date, broken by a failure **or a missing run** |
| Breakdown | each Routine's own streak |
| Context | total successful runs all time, which only ever rises |
| **Trend** | the streak's own history — how long previous streaks ran before breaking |

The streak drops to zero on any failure, which is the point: it reflects current
health rather than accumulated volume. The all-time total is kept alongside it as
context, not as the headline, because a number that only rises cannot tell you
something broke this week.

**The combined streak interleaves Routines by date**, so a cost Routine failure
resets it even though the race report kept working. That is intended for the
headline — it answers "when did anything last break" — and it is why the per-Routine
breakdown sits next to it. Read the headline for whether the automation is healthy
and the breakdown for which part is not.

A Routine's own runs count toward this, the scoreboard Routine included. It is
measuring the automation, and it is part of the automation.

**Source:** the `ok` field across every Routine's ledger prefix in GCS, one object
per run. Sort all rows by date and count back from the newest.

**A backfill row is not a run.** Rows carrying a `backfill` object were
transcribed from past reports (`operations/ledger/README.md`, Backfill) and hold
`ok: null`. They count toward the outcome split and are skipped by the streak
entirely. They neither extend it nor break it, and a scheduled date covered only
by a backfill row is outside the walk, not a gap in it.

**Counting rows alone overstates the streak.** The streak breaks on an `ok: false`
row *or* on a missing one. A Routine that dies before writing anything leaves no
row at all, and a walk back through the rows that exist steps straight over that
morning and keeps counting. `operations/ledger/README.md` already defines a missing
date as a missed run; the streak has to apply that rule rather than trusting the
rows to be complete.

So: build the list of dates each Routine was scheduled to fire, walk back from
**the latest completed scheduled run**, and stop at the first date that is either
`ok: false` or absent. Absence is the failure mode most worth catching, because it
is what a crashed or never-started run looks like.

**Not from today**, and the distinction is load-bearing. The scoreboard Routine
derives the metrics before it appends its own row for that day, so at derivation
time today's scoreboard row is always absent. A walk starting at today would read
its own in-progress run as a missing one and return a streak of zero, every single
day. An in-progress run is neither absent nor failed; it is not yet a fact.

**A run cannot always score itself.** A race report that misdiagnoses the morning
believes it did fine — that is exactly what 2026-09-15 was. So `ok` written by the
run being scored is provisional.

Git settles this in one direction only, and an earlier draft of this document got
it wrong. A later commit correcting a published report establishes `ok: false` for
that run: the output needed correction, and here is the correction. **The absence
of such a commit establishes nothing.** It is consistent with a correct report, and
equally consistent with one nobody has read closely enough to correct — which is
the more likely reading for a report that merged itself at 06:48 and was never
opened again.

So the derivation gives:

| Evidence | `ok` |
|---|---|
| a later commit modified the report | `false`, with the correcting commit as the note |
| the run reported an environment failure or a wrong-hour fire | `false` |
| nothing | **`unknown`** |

`unknown` is not `true`. A streak computed over unknowns is a claim about how much
has been checked, not about how much worked, and the page must not present it as
the latter. This is the same rule `operations/ledger/README.md` applies to
backfill: a missing row is detectable, an inferred row presented as read is not.

## 3. Cost

| Figure | |
|---|---|
| Headline | this month's spend |
| Also | $/booking |
| **Trend** | this month against last month |

$/booking is the input to the Cloud SQL retention decision open since #41 and #168
(~$9.50/month). Its divisor is `exact + fallback` from `race-report.jsonl` and
needs no new source.

**Source:** `cost.jsonl`, written by the cost Routine. **Not yet available.** The
billing export is not enabled. `terraform/cost.tf` creates the dataset it writes
to and grants the Routine read access; turning the export on is a Console step on
the billing account. #227 covers why BigQuery is the only path to the figure, and
`operations/routines/cost.md` the remaining steps.

**Scope is GCP only, and the page must say so.** Whether Anthropic agent and token
spend can be measured is open in #228. For a project developed agentically against
a ~$10/month GCP bill, the omitted half is plausibly the larger number, so a figure
presented without its scope would understate total spend rather than approximate
it.

---

## Excluded

**Commit count, PR count, lines changed.** These measure activity rather than
outcome, and are directly gameable by automation that generates its own work.

**Malformed response rate** and **fix latency** were specified and cut, to keep
this to what is worth maintaining at current volume. Neither was instrumented. The
incidents that motivated the first — a member sent a Selenium stack trace, a member
sent a message addressed to someone else, and a member told a result would follow
who received nothing, all on 2026-09-17 — remain in
`operations/race-reports/2026-09-17.md`. They are no longer tracked as a rate.

**Human-touch rate** — the proportion of merged PRs where the maintainer edited
code rather than only approving — was also cut. It measured this project's premise
most directly, and it is the one excluded metric whose input cannot be
reconstructed after the fact. Nothing is accumulating it.

## The page is public

`docs/` is served as GitHub Pages and the repository is public, so the scoreboard
is public. Counts, rates and spend are fine. Member identifiers are not: no names,
phone numbers or Telegram handles, on the page or in any ledger feeding it.

## How it is built

| | |
|---|---|
| Source routines | write their own metrics to their own ledger |
| Scoreboard Routine | reads those ledgers, derives the three metrics, writes `docs/scoreboard.json`, appends to `scoreboard.jsonl` |
| `docs/scoreboard.html` | static page, written once; reads the JSON |

Splitting the data from the page keeps each update to one JSON file and one
appended ledger line, so what changed is visible in the diff rather than buried in
a re-rendered page.

Specified in `operations/routines/scoreboard.md` and
`operations/routines/cost.md`. Neither is deployed.

## Current state

**The outcome split has a value, backfilled.** The page shows 23 mornings
transcribed from the race reports (2026-08-13 to 2026-09-27), derived by
`operations/ledger/derive_scoreboard.py` and flagged as backfilled on the page.
The streak has a value from 2026-10-01, the race report Routine's first row, and
every run in it is self-reported (§2). Cost has no source at all.
