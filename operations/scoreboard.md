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

**Exact and Fallback are not yet distinguishable.** Separating them requires the
member's request to have been resolved to a bookable slot and confirmed before the
race, rather than selected from a ±32-minute window at race time. That step does
not exist; it is #216. A request for "8am" against a sheet with no 08:00 slot has
no reference value, so until #216 ships the honest report is Miss against not-Miss,
and the page must say so rather than showing an Exact count that means something
weaker than it reads.

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
| Headline | the combined streak: consecutive successful runs, all Routines interleaved by date |
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

**Source:** the `ok` field across every ledger in `operations/ledger/`. Sort all
rows by date, count back from the newest to the first `ok: false`.

**A run cannot always score itself.** A race report that misdiagnoses the morning
believes it did fine — that is exactly what 2026-09-15 was. So `ok` written by the
run being scored is provisional. Where a later commit corrected a published report,
that is the evidence: `ok` for a race report can be derived from git, since a
report no later commit modified was not corrected. Deterministic, needs no
judgment, and works retroactively.

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
project has no billing export and no BigQuery dataset, and no service account in
`terraform/` holds a billing role. #227 covers what enabling it requires, and why
BigQuery is the only path to the figure.

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

**No metric has a value.** Nothing writes any ledger, the page does not exist, and
cost has no source at all. This document and the two Routine specifications are
the work remaining.
