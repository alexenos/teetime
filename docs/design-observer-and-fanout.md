# Design: the observer job, and the per-member fan-out

**Status:** Part A (phase 0) built — see "Phase 0 as built" below. Part B still
draft, for review. No change to the booking path in phase 0 or 1.
**Date:** 2026-09-11
**Companion:** `docs/booking-post-mortem-2026-09-11.md`

---

## Why this exists

Four consecutive Fridays the requested slot was lost. The blocker is not that
we lack a fix — it is that **two incompatible explanations both fit every
artifact we have**, and they call for opposite fixes:

- **Model L — the gate is late.** Friday's sheet opens around club `:05`–`:06`.
  Our early refusals say nothing about who holds the slot, and we stop asking
  just before it becomes winnable. → *keep asking the target longer.*
- **Model F — the rival is fast.** The gate opens at `:01` as on every other
  day, and the target is gone before our first ask at +1018ms. The fallbacks
  sat free the whole time. → *reach the fallbacks sooner.*

We cannot separate them from our own requests. A refusal's body is a re-render
of our own pre-window snapshot — its rows, countdown and sheet-open marker
describe *us*, not the club (§7d of the post-mortem skill). The only live bit
is the verdict, and a verdict on one slot at one instant cannot distinguish
"shut" from "taken". Every Friday we have run is one slot asked repeatedly,
which is precisely the case that proves nothing.

An independent reader of the sheet breaks the tie — and keeps breaking it,
every morning, for free.

## What is already settled

- **Concurrent sessions on one credential are fine.** A member sits logged in
  watching while the bot logs in at 06:28 and books; the older session is never
  kicked. The observer needs no second account.
- **The target is genuinely open pre-window.** In the frozen pre-window view
  every Northgate slot renders `Empty` with a Reserve button, while the second
  course already shows real reservations. The renderer *does* show holdings, so
  the slot is won during the race, not pre-placed.
- **ViewState is reusable, not single-use.** All twelve of 09-11's Reserves
  carried one ViewState and all twelve were processed — but sharing it across
  two grants is what mis-anchored a booking on 09-04 evening.
- **Stale staging is not the Friday cause.** The winning fallback rode the
  identical ViewState (`d249ba63`) as the twelve refusals, the index→time map
  was unchanged from pre-window to +38s, and every refusal came back as the
  club's real popup path rather than an expired view. Only the slot differed.

---

## Part A — the observer job

A separate Cloud Run job that logs in, parks on the target date, and
photographs the tee sheet once a second across the window. **It never sends a
Reserve.**

### Why it cannot live inside the racer

1. **Reserve bodies address slots positionally, against a view someone else
   could re-point.** The date rides in session state, not the URL
   (`walden_http.py:893`), so a plain `GET` renders *today's* sheet and
   observing the target date means driving the date-selection AJAX. That is
   categorically unlike a Reserve POST: a Reserve hands the same ViewState
   straight back, but a date change re-renders the view for another date — and
   `teeTimeSlots:11` means "the twelfth row of whatever date this view shows".
   Flip the shared view mid-race and we reserve the wrong tee time with no
   error at all.
2. **Chrome is the measured CPU thief.** A co-resident browser was caught
   taking 310ms from one attempt (`cpu/wall 0.39`, 08-28), and post-response
   processing is already 44% of the race budget (§7c). A second browser in the
   racing container attacks the exact three seconds that matter.
3. **Shared ViewState already cost a booking once.** Structural separation is
   cheaper than discipline.

### Fail-safe by ordering

The observer logs in **before** the racer — 06:26 against 06:28. If the club
ever did start enforcing one session per member, the newer login wins and the
casualty is the observer, never the booking. This holds without relying on the
concurrency evidence above.

### Shape

- Own job, own container, own browser, own session. Same credential.
- **Runs every morning**, scoped to a single booking job — currently the
  founding member's. Non-Friday mornings are the control group, and they come
  free.
- Reads the due booking to learn which date to watch, so it always parks on the
  same sheet that job will race for.
- Warm and logged in by 06:26; date selected; idle until the window.
- Snapshots at `+0s` through `+8s`, one per second, on its own connection.
- Raw bytes to GCS **unparsed** during the race — parsing is post-hoc, so the
  observer's own CPU cost stays near zero while the race runs.
- No Reserve code path linked into the job at all. Read-only by construction,
  not by policy.

### Output

`walden/observer/<date>/snapshot_+NNNNms.html`, plus an `observations.jsonl`
of `{ tMs, slotTime, slotIndex, state, holders }` rows derived afterwards.
Mirrors the race ledger's conventions so the post-mortem skill can read both.

### Phase 0 as built (issue #189)

Implemented in `app/observer/` as the Cloud Run job `teetime-observer`
(`terraform/observer.tf`), scheduled at 06:24 CT every morning. Three details
differ from the sketch above and one is load-bearing.

**The sheet is re-requested before every snapshot.** This is the correction
that matters. A browser parked on the tee sheet and asked for its
`page_source` nine times hands back nine copies of its own pre-window DOM — the
identical trap that makes the racer's refusal bodies useless as evidence
(§7d: the verdict is live, the body is not), and a run shaped that way would
tick every box in the issue while proving exactly nothing. So each tick clicks
the already-selected day tab, whose `PrimeFaces.ab` handler carries
`u:` = the whole tee time form, and waits for the old element to go stale
before reading. That is the same element the direct-HTTP booker replays for its
own view refresh, and it re-renders the same date rather than changing it.

Consequently a snapshot is named for the offset at which its **re-render was
requested**, not when the bytes were read: that is the instant the returned
sheet describes. `manifest.jsonl` carries both, so the two can be read against
the ledger's `sentMsPastWindow` + `roundTripMs` bound on one clock.

**No course is selected.** The racer selects Northgate because a Reserve is
addressed against the form's selected course. The rendered sheet is not
filtered by it — 09-04's captured pre-window sheet carries 1052 row ids under
`teeTimeCourses:0` and 686 under `:1` — so the observer skips the ~400 lines of
course-dropdown fallbacks and records `northgateRowCount` from the parked sheet
instead, which makes a sheet that somehow lacks the contested block say so in
its own run record.

**Object layout.** `walden/observer/<target date>/<run id>/`, holding:

| object | contents |
|---|---|
| `snapshot_+NNNNms.html` | raw page bytes, unparsed, one per tick |
| `manifest.jsonl` | per snapshot: planned / sent / settled / captured offsets, byte count, `refreshOk`, and a note when a re-render did not land |
| `run.json` | target date, the window instant, the day tab's own text, `northgateRowCount`, readiness offset, counts captured and stored |

The run id is a UTC `%Y%m%d_%H%M%S` stamp — taken when the job *starts*, so it
reads as the 06:24 execution rather than the ~06:30:08 the last snapshot lands
at — followed by the Cloud Run execution id. Two executions of a Cloud Run job
can overlap, and the bucket is written with `objectCreator`, which grants create
but not delete: a colliding prefix would not merge, it would make the second
run's uploads fail. The execution id also ties a directory back to exactly one
run in the console. It is nested under the target date rather than replacing it
so that a dry run and the real morning can watch the same sheet without
overwriting each other. Deriving `observations.jsonl` — `{ tMs, slotTime,
slotIndex, state, holders }` rows — is post-hoc work and deliberately not in
this job; parsing a 670KB sheet costs ~37ms and none of that may land inside
the window.

**Losing the bytes is a failed run.** Each upload is independent, so one
failure does not cost the other eight and a partial morning still separates the
two models. But storing *nothing* exits non-zero: the only product of this job
is evidence, and a run that captured nine snapshots, lost every upload and
exited 0 would be indistinguishable from a good one until someone went looking
for the bytes a Friday later.

**What it refuses to do.** If it cannot confirm the view is on the target date,
it captures nothing and exits non-zero. Bytes from the wrong date carrying the
right date's name would be read by a later post-mortem as evidence, which is
worse than an empty morning — and an observer that gives up costs nothing,
because it never had a tee time at stake.

**Dry run**, any time of day — the waits are no-ops once the window has passed,
so the nine snapshots simply bunch and the whole path still executes:

```bash
gcloud run jobs execute teetime-observer --region=us-central1 \
  --project=gen-lang-client-0822973627 --wait
```

### What the first Friday settles

| snapshot | if Model L (late gate) | if Model F (fast rival) |
|---|---|---|
| +1s | block untouched | **target already taken** |
| +2s | block untouched | block filling |
| +5s | **first holdings appear** | mostly filled |
| +8s | block filling | settled |

Either way we also learn whether the fallback was free at `:01` — that is, what
twelve asks on a dead slot actually cost.

---

## Part B — per-member fan-out

One job execution per member booking: own container, own credential, own
browser, own session. Nothing shared at race time.

### Why separation is structural, not stylistic

- **Cross-contamination becomes impossible.** Two asks sharing one ViewState
  already anchored a reservation to the wrong slot once. Two *members* in one
  process is that hazard with someone else's Friday at stake.
- **Every member's work lands on the same instant.** This is the worst possible
  shape for a shared container — N browsers and N 660KB parses contending for
  one vCPU inside the same three seconds, when own-CPU already runs ~3× the
  idle benchmark.
- **Blast radius.** One dead view, one timeout, one crashed browser should cost
  one booking, not all of them.

### Decisions

| concern | bites at | proposal |
|---|---|---|
| Warm-up | N ≥ 1 | A cold start at 06:29 loses outright. Min-instances, or a pre-warm ping at 06:20. |
| Rate limiting | N ≥ ~5 | At the N=10 ceiling that is 120–160 POSTs inside 3s from one egress range. The ledger already captures `statusCode`, `retry-after`, `x-ratelimit-*`; branch on them. Plan to cut per-member burst density as N grows — 10 × 4 is a very different footprint from 10 × 12. |
| Shared fate | N ≥ 2 | Separate jobs are separate *processes*, not separate *systems*: one IP range, one fingerprint, one instant, and every request authenticated as a known membership. A throttle or a flag takes out every member at once, including the one booking that works today. |
| Credential isolation | N ≥ 2 | Per-member credentials already live in the encrypted store (#179 phase 1). One credential never crosses a container boundary. |
| Target collision | N ≥ 2 | Deferred by decision. Note that past N≈5 members increasingly take slots from each other in the same prime block. |
| Observability | N ≥ 2 | Keep the per-member ledger shape identical so the post-mortem skill reads N mornings as easily as one. |

### Explicitly not doing

**Distributing jobs across source IPs.** No rate limit has ever been observed —
not one non-200 in any ledger in the bucket — so it would be building against a
limit of unknown existence and unknown key, and the key is at least as likely
to be the member account or a global endpoint budget as the source IP. It also
would not achieve separation: every request is authenticated, so ten
memberships firing identically-shaped bodies within milliseconds of the same
instant correlate on account and timing regardless of source address.

---

## Sequence

| phase | work | note |
|---|---|---|
| **0** | Observer job | Read-only, separate job, existing credential. Zero booking risk, and a working pilot of the fan-out shape before anyone's tee time depends on it. |
| **1** | Extract the booking job; surface throttle signals | Invocable once per member with an injected credential. Existing member runs through it unchanged: behaviour identical, topology new. |
| **2** | Fan out | Scheduler dispatches one execution per due booking. 1 → 2 first, ceiling around 10. |
| **3** | Revisit the opening, informed | A dense cluster straddling the tick if the rival is milliseconds fast; sustained target asks if the gate really is late. One of those, not both, and not before the data. |

**No change to the race strategy in Phase 0 or 1.** The burst stays twelve
members, the aim stays at the tick, the fallback walk stays as it is. Changing
strategy and instrumenting it in the same week means we learn nothing from
either.

### Timing

Next Friday is **2026-09-18**. Phase 0 wants to be deployed and warm before
06:28 CT that morning or Friday data waits another week. Note that a commit to
`main` redeploys the booking service, so the merge wants to land comfortably
ahead of Friday rather than the night before.

## Open

**What the club's terms of use say about automated booking, and about one
operator running it for ten memberships.** Separate jobs do not make these
separate systems in any sense the club would perceive, and the consequence of
getting it wrong is shared across every member. Worth settling before the step
from 2 to 10, not after.
