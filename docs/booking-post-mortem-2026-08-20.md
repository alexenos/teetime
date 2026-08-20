# Booking post-mortem: 2026-08-20

**Target:** Thursday 2026-08-27, 08:08 AM, 4 players, Northgate
**Window:** Thursday 2026-08-20 06:30:00 CT (job fires 06:28)
**Outcome: won.** Granted on the first Reserve at +1006ms, chain completed, and
`RESERVATION_CHECK` found `08/27/2026 08:08 AM - 08:15 AM RESERVED` on the
member's own reservations page.
**Code that ran:** `5e0f12d` (#154), merged 2026-08-18 10:16 CT. Nothing has
merged since, so `main` is what executed — no revision archaeology needed.

**Status: this morning is closed.** The two mornings before it are not, and they
are the reason this document is longer than one paragraph.

---

## This morning, in order

| Stage | What happened |
|---|---|
| Login → sheet | Steps 1-4 clean, 06:28:15 → 06:28:49 |
| Slot scan | 143 rows → 9 candidates (dropped course=64, window=70) |
| Pre-locate | 08:08 AM, `index=7`, `exact=True`, `available=4`, **8 fallbacks** |
| Clock | club +12ms, one-way 12ms, 104 probes, 5 transitions, **tick pinned to ±23ms** |
| Lead | 24ms early |
| Fire | Reserve **1/8** at **+1006ms** past the window (aim +1030, less the 24ms lead) |
| Answer | **accepted** — club clock **+1001ms**, round trip **456ms**, 84077 bytes, 0 sheet rows, 0 Reserve buttons, `form=08:08 AM`, `popup=False` |
| Ledger | `club granted 08:08 AM at +1006ms past the window` |
| Outcome | `phase=complete, success=True, attempts=1, totalMs=1870` |
| Verify | `RESERVATION_CHECK: Reservation found` |

The `<eval>` on the accepted response is the accept signature and carries no
`.show()`:

```
executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;
```

Artifacts: `gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260820_113002/`
(`ledger.jsonl`, `attempt_01_accepted.xml`). One attempt, so one envelope — the
fallback list was never touched.

**The exact slot was requested and the exact slot was booked.** No fallback, no
second rung, no refusal anywhere in the ledger. This is the second race (after
2026-08-16) to win on attempt 1 since #150 moved the aim to +1030ms.

### The boundary held again

`serverMsPastWindow: 1001` with a **456ms** round trip brackets the club's
receipt inside `[+1006, +1462]` — the 06:30:01 second, the same second that has
granted every accepted Reserve on record. Per §7a's caveat this row *can* carry
the boundary argument, because the round trip is short enough to bracket it:

| morning | sent | club second | verdict |
|---|---|---|---|
| 08-15 | −60ms | 06:30:00 | refused |
| 08-15 | +1240ms | 06:30:01 | accepted |
| 08-16 | +1023ms | unbracketable (2935ms round trip) | accepted |
| **08-20** | **+1006ms** | **06:30:01** | **accepted** |

Still no evidence the 06:30:01 boundary drifts. The ladder brackets it either
way.

### The 3.0s margin: 08-16 was an outlier, not a trend

§7b asked for `roundTripMs` on every race. Adding the two ledgers written since:

| morning | kind | roundTripMs |
|---|---|---|
| 08-13 | race | 593 |
| 08-14 | race | 647 |
| 08-14 | ad-hoc | 956 |
| 08-15 | race | 741 / 754 |
| 08-15 | ad-hoc | 942 |
| 08-16 | race | **2935** |
| 08-18 (evening) | ad-hoc | 522 |
| **08-20** | **race** | **456** |

456ms is the **fastest round trip on record**, race or ad-hoc. 08-16's 2935ms
now stands alone against seven values between 456 and 956, so it reads as a
one-off rather than a drift toward `_RESERVE_TIMEOUT_S`. **Recommendation:
leave the 3.0s timeout alone.** Keep reading the field; a second morning above
~2s would change the answer, and one still has not appeared.

---

## The two mornings before this one

Both were found while collecting round trips for the table above. Neither was
noticed at the time, and neither is about the club. Only one of them cost a tee
time: 08-18. 08-19 had nothing scheduled, and the failure there is a latent bug
rather than a loss.

### 2026-08-19 — the job 500'd before it ever looked for a booking

**Nothing was lost.** The member confirms no booking was scheduled for that
morning, and the daily job is a no-op on those days. What follows is a latent
bug that surfaced on the one kind of day where it cost nothing.

```
11:28:04 POST /jobs/execute-due-bookings HTTP/1.1" 500 Internal Server Error
asyncpg.exceptions._base.InterfaceError: connection is closed
  → app/api/jobs.py:194   due_bookings = await booking_service.get_due_bookings(...)
  → app/services/database_service.py:246   result = await db.execute(query)
```

Cloud Scheduler fires this endpoint every morning whether or not anything is
due. The failure is on the job's **first** DB query, so it died before it could
determine there was nothing to do. Nothing else ran that morning — the logs from
06:20 to 12:00 contain that one request and no retry.

**Root cause, established from the code:** `app/models/database.py:113` builds
the engine with neither `pool_pre_ping` nor `pool_recycle`:

```python
engine = create_async_engine(
    settings.database_url... ,
    echo=False,
)
```

Postgres (or an intermediary) drops the pooled connection while the instance is
idle, SQLAlchemy hands out the dead one, and the first query raises `connection
is closed`. There is no pre-ping to detect it and no retry above it.

**The idle gap was ~9h19m, not overnight.** Last DB activity was 02:09 UTC
(21:09 CT on 08-18), when the member booked 2026-08-23 ad-hoc over Discord — the
`20260819_020906` ledger. Between 02:10 and 11:28 UTC the logs are empty. So the
connection died inside a nine-hour gap, which is well within ordinary
idle-timeout territory and does not need a 24-hour story.

**It is rare, not chronic.** Across 29 scheduler invocations from 07-22 to
08-20, this is the only failure — 28 × `200 OK`, one `500`, and `connection is
closed` appears exactly once in a month of logs. Treat it as an intermittent
stale-connection race, not a daily hazard.

**Fix:** `pool_pre_ping=True` (and a `pool_recycle` shorter than the idle gap).
One line, no morning needed to test it — this is a pool behaviour, not club
behaviour. Cheap insurance rather than an emergency: rare, but the failure mode
is total, and on a morning that *did* have a booking it would have killed the
run before the browser ever opened.

### 2026-08-18 — a booking was due, and the window passed during login

One booking due (2026-08-25). It never reached a Reserve:

```
11:28:08  STARTING BATCH BOOKING  date=2026-08-25, num_requests=1
11:28:18  Step 1 - Logging in
11:29:06  Entering credentials...          (48s just to load the login page)
11:31:08  urllib3 WARNING - Retrying ... after connection broken
11:32:16  Login successful                 (window opened 2m16s ago)
11:33:07  BATCH_JOB: Batch execution timed out
11:33:23  Step 3 - Selecting course → element not interactable
11:33:25  Date selection failed → BATCH BOOKING COMPLETE
```

Login alone took **3m58s** against a ~110s budget before the window. The
`asyncio.wait_for` in `app/api/jobs.py:218` (`BOOKING_EXECUTION_TIMEOUT_SECONDS`
× 1 booking = 300s) fired at 11:33:07 while the provider was still walking the
sheet. Classification: **lost on infrastructure**, upstream of every stage the
skill's §6 taxonomy covers. The club was never asked.

The urllib3 "connection broken" retry points at the Cloud Run instance's network
or a cold container rather than at Walden — but a single morning does not
separate those, and unlike the 08-19 bug this one has no obvious one-line fix.
It is the weaker of the two findings and is recorded, not diagnosed.

---

## Correction to the post-mortem skill

**`targetTimestampMs` in the ledger is not a pre/post-#150 marker.** §1 reads
08-15's `06:29:59.999` as evidence the run predated #150. It is not: the field
records the *stated window*, deliberately and on every run, so that ledger
offsets stay on one scale as the aim moves —
`walden_provider.py:4261-4266` says so outright ("The stated window, not the
aim"). This morning's ledger, on code two commits past #150, decodes to the same
`06:29:59.999`.

(The 1ms shortfall against a true 06:30:00.000 is truncation:
`window_timestamp_ms = int(time.time() * 1000) + delay_ms` at
`walden_provider.py:1310` truncates twice. It is ≤1ms, it does not move the aim
in any way that matters, and the Step 7 log prints the true `execute_at_ct`
rather than the reconstruction — which is why the two disagree.)

08-15's conclusion still stands; it was independently carried by the absent
`RESERVATION_CHECK` line. Only the `targetTimestampMs` half of that argument is
withdrawn. **Use `RESERVATION_CHECK` presence and the rung offsets to date a
run, never this field.**

---

## Recommendations

1. **`pool_pre_ping=True` on the engine** (`app/models/database.py:113`), plus a
   `pool_recycle` below the observed idle gap. Fixes the 08-19 class outright.
   Cheapest fix on this page and costs no morning to validate. Not urgent — one
   occurrence in 29 invocations, and that one cost nothing — but the failure
   mode is total, so it is worth doing before it lands on a booking morning.
2. **Alert on a job that does not reach `BATCH_JOB:` or `BATCH COMPLETE`.** Both
   08-18 and 08-19 failed silently; this morning's win was found because it was
   asked about, and those two losses only because a round-trip table needed
   filling. A 500 on `/jobs/execute-due-bookings`, or a batch that times out,
   should page rather than sit in the logs.
3. **Leave `_RESERVE_TIMEOUT_S` at 3.0s.** See the table above — 08-16 is one
   point against seven, and this morning was the fastest yet.
4. **No change to the race path.** It has now won three of the last four races
   it was allowed to run, twice on attempt 1. Nothing in this morning's ledger
   argues for touching the ladder, the aim, or the clock probe.
5. **Reconcile the "828ms" comment in `walden_http_booker.py`** with the ledger
   table, which still has no such row and now spans 456-2935ms. Carried over
   from §7b; unchanged this morning.
