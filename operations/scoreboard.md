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

Consecutive clean runs of the race report cycle.

The race report is the only automation this metric covers, because it is the
only cycle that runs on a schedule and acts without being asked. `ship-pr` and
the PR check-ins are maintainer-invoked and are not counted here.

**Clean** means the run's output required no correction before it could be
acted on. It is independent of the outcome the run reported: a correctly
diagnosed loss is clean, and so is a correctly reported "no booking was
scheduled".

The cycle now commits and merges its own report
(`operations/cycles/race-report.md`), so an incorrect report reaches `main`.
A report that merged and then required a correction commit is not clean.

Two recorded cases, both predating that authorization — no auto-merged report
has yet required a correction:

| Date | Why not clean |
|---|---|
| 2026-09-15 | Reported no booking on a morning when two bookings succeeded. The log query was scoped to the Cloud Run service and excluded the jobs. Identified by maintainer pushback; fixed in #206. |
| 2026-09-18 | The report's headline finding, a "112ms escape" against `_RESERVE_TIMEOUT_S`, was wrong about which timeout applies to a burst fire. Retracted by #219 the following day. |

**Source:** `operations/ledger/runs.jsonl`, counted back from the newest row to
the first `clean: false`. Do not store the streak.

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
outcome, and are directly gameable by a cycle that generates its own work.

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

The race report cycle emits rows; the scoreboard is a rollup over those rows.
It does not parse the report documents. See `operations/ledger/README.md`.

The reports in `operations/race-reports/` are unchanged by this. They hold the
analysis; the ledger holds the values used for computation.

## Current implementation status

No cycle emits rows, and no rollup exists. The ledger files are empty and this
document is a specification. `operations/ledger/README.md` states the write
path and the work required to populate it.
