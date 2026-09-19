# The scoreboard

Five metrics. Three measure member outcomes, one measures the operating
system, one measures cost.

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
establish a reservation. This distinction is recorded in the post-mortem skill
and is the most likely element of this definition to regress.

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

## 2. Malformed response rate

Proportion of member-facing messages containing raw error text, sent to the
wrong recipient, or otherwise unusable.

```
malformed ÷ total member-facing messages sent, 30-day rolling
```

Report the raw pair alongside the percentage: "2 of 34 (5.9%)". At current
volume one message moves the rate by several points.

**Source:** not instrumented. Neither numerator nor denominator is currently
counted.

Reference case, 2026-09-17: of three requests, one member received a Selenium
stack trace, and one received a message addressed to a different member.

**Known gap — unsent messages.** On the same morning, one member was told a
result would follow and received nothing: the notification hit a Telegram
`ConnectTimeout` and was dropped. An unsent message does not appear in a ratio
over messages sent. From the member's position it is indistinguishable from a
booking still in progress. Either extend this metric to cover messages that
should have been sent, or add a sixth metric. Not decided.

## 3. Fix latency

Median of the last 5.

```
start: a post-mortem or review identifies the fix
end:   that fix is on main
```

The start condition determines the value. Without it the metric measures time
from defect introduction to fix, which is a different quantity: the 2026-09-17
browser contention measures a few hours under the first definition and months
under the second.

**Source:** git, plus post-mortem document dates. No new instrumentation
required.

## 4. Autonomy streak

Per cycle: current rung (R0–R5) and consecutive clean runs at that rung.

The only metric measuring the operating system rather than the application.
Defined in `operations/autonomy.md`, including the definition of "clean" and
the scoring-authority constraint on promotion.

**Source:** `operations/ledger/runs.jsonl`, counted back to the last
`clean: false`.

## 5. $/month

Total spend, and $/booking.

$/booking is the input to the Cloud SQL retention decision open since #41 and
#168 (~$9.50/month). Include agent and token spend, not GCP alone.

**Source:** not available. Requires billing export access; the post-mortem
service account is scoped to storage and logging, which is the same limitation
that left the memory-versus-CPU question unresolved on 2026-09-17.

---

## Excluded

Commit count, PR count, lines changed.

These measure activity rather than outcome, and are directly gameable by a
cycle that generates its own work.

## Undecided

**Human-touch rate** — proportion of merged PRs where the maintainer edited
code rather than only approving. Measures the project's original premise
directly. Not adopted.

## Computation

Cycles emit rows; the scoreboard is a rollup over those rows. It does not
parse the post-mortem documents. See `operations/ledger/README.md`.

The post-mortem documents are unchanged by this. They hold the analysis; the
ledger holds the values used for computation.

## Current implementation status

No cycle currently emits rows, and no rollup exists. The ledger files are
empty and this document is a specification. See `operations/ledger/README.md`
for the write path, and the open work required to populate it.
