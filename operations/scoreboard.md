# The scoreboard

Five numbers. Three say whether the product works for the members, one says
whether the operating system is taking work off the maintainer, one says what
it costs.

Every metric names the field it is computed from. A metric without a stated
source drifts, and this project has already been burned by a metric-shaped
answer with no provenance — a valid, zero-row log query read as "no booking
ran" on 2026-09-15.

---

## 1. Outcome split

Every booking request lands in exactly one of three buckets. Per **request**,
not per morning. Rolling 4 race mornings.

| Bucket | Means |
|---|---|
| **Exact** | got the tee time the member agreed to |
| **Fallback** | got a tee time, but not that one |
| **Miss** | no reservation at all |

**Source:** `RESERVATION_CHECK`, and nothing else. `phase=complete,
success=True` is not proof a tee time exists. This correction is in the
post-mortem skill already and is the single easiest thing to quietly re-break.

**What makes it Exact:** the member's request must have been resolved to a
real, bookable slot and confirmed with them *before* the race was scheduled.
Not the nearest thing inside a ±32-minute window the bot chose on their behalf.

That confirmation step does not exist yet — it is **#216**. Until it ships,
Exact and Fallback cannot be honestly separated, because "8am" against a sheet
with no 8:00 on it has no ground truth to compare against. Report the split
with that caveat attached, or report Miss vs. not-Miss, but do not quietly
score a fallback as exact.

**Why per request:** 2026-09-15 was two requests, two Exact. 2026-09-17 was
three requests, three Miss. Per-morning scoring flattens both.

## 2. Malformed response rate

Share of member-facing messages that went out wrong — raw error text, wrong
recipient, unusable content.

```
malformed ÷ total member-facing messages sent, 30-day rolling
```

**Always show the raw pair alongside the percentage** — "2 of 34 (5.9%)". At
this volume the denominator is small enough that one bad message moves the rate
several points, and a percentage alone hides that.

**Source:** needs instrumentation. Neither the numerator nor the denominator is
counted today.

Worked example, 2026-09-17: three members asked for tee times, one received
nineteen frames of a Selenium stack trace, one received a message meant for
someone else.

**Known gap — the silent case.** That same morning, one member was told "I'll
message you with the result" and never heard back: the notification hit a
Telegram `ConnectTimeout` and was dropped. That is an *absent* response, not a
malformed one, and it will never appear in a ratio over messages actually sent.
It is also the worst failure mode, because the member cannot tell it apart from
a booking still in progress. Either widen this metric's definition to cover
"should have been sent and wasn't", or add it as a sixth. Undecided.

## 3. Fix latency

Median of the last 5.

```
clock starts: a post-mortem or review identifies the fix
clock stops:  that fix is on main
```

**Pin the start of the clock.** Without it this drifts toward "time from bug to
fix", which is a different and much larger number — 2026-09-17's browser
contention reads as a few hours under one definition and months under the
other.

**Source:** git, plus the post-mortem doc's date. Available today with no new
instrumentation.

## 4. Autonomy streak

Per cycle: current rung (R0–R5) and consecutive clean runs at it.

The only metric here about the operating system rather than the app. Defined in
full in `operations/autonomy.md`, including what "clean" means and why a cycle
cannot outrun its scorer.

**Source:** `operations/ledger/runs.jsonl`, counted back to the last
`clean: false`.

## 5. $/month

Total spend, and **$/booking**.

$/booking is what makes this feel like a business rather than a hobby, and it
is the number that either justifies or retires the ~$9.50/month Cloud SQL
instance — a decision open since #41 and #168. Include agent/token spend, not
just GCP.

**Source:** needs billing export access. The post-mortem service account is
scoped to storage and logging only, which is the same gap that left the
memory-vs-CPU question unresolved on 2026-09-17.

---

## Deliberately not on the board

**Commit count. PR count. Lines changed.**

They measure activity rather than outcome, and they are the easiest thing for
an autonomous system to game. An agent loop that opens PRs to raise its own
number is a real failure mode once cycles are driving themselves, not a
hypothetical one.

## Still undecided

**Human-touch rate** — the share of merged PRs where the maintainer edited code
rather than only approving. It is the most direct read on this project's
founding thesis ("I don't have to look at any of the code"), which is an
argument for it. Not yet adopted.

## How it gets computed

Cycles **emit rows**; the scoreboard is a rollup over them. It never parses
nineteen markdown post-mortems — see `operations/ledger/README.md`.

The prose post-mortems stay exactly as they are. They carry the reasoning; the
row carries the fact.
