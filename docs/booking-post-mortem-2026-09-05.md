# Booking post-mortem: 2026-09-05

Target booking date: **2026-09-12** (Saturday), 12:08 PM, 4 players, Walden Northgate.

## 1. Verdict: WON, first rung

The club granted 12:08 PM on attempt 1/6 (burst member #0), sent at
+1010ms past the window, club server-side clock at +1001ms.
`RESERVATION_CHECK` independently confirmed it on the member's
reservations page:

```
TEE TIMES (NORTHGATE) 09/12/2026 12:08 PM - 12:15 PM RESERVED
```

`evalText` from the winning row is
`executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;`
— the accepted-verdict signature. The opening burst sent 6 asks (all
against the target slot only, per PR #174's "burst asks only the
target"); attempt 1 won and 6 more were skipped after the grant, except
one (#5) that was already in flight.

Race ledger (`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260905_113005/ledger.jsonl`):

| attempt | burstIndex | slot | sent | verdict | roundTripMs | server clock |
|---|---|---|---|---|---|---|
| 1 | 0 | 12:08 PM | +1010ms | **accepted** | 806 | +1001ms |
| 2 | 1 | 12:08 PM | +1110ms | refused | 548 | +1001ms |
| 3 | 2 | 12:08 PM | +1230ms | refused (frozen dup) | 518 | +1001ms |
| 4 | 3 | 12:08 PM | +1380ms | refused | 845 | +1001ms |
| 5 | 4 | 12:08 PM | +1530ms | refused | 682 | +1001ms |
| 6 | 5 | 12:08 PM | +1710ms | accepted (surplus hold) | 509 | +2001ms |

One note, not a failure: burst member #6 was also granted 12:08 PM — a
"surplus hold" per the booker's own log line, left to the club's hold
timer. This is the same surplus-grant behavior seen on 2026-09-04
evening (`docs/booking-post-mortem-2026-09-04-evening.md`), but that run
mixed target and fallback slots and the club ended up finalizing the
wrong one. Here, because PR #174 restricted the burst to target-only
asks, both grants were for the same slot (12:08 PM) — no cross-slot risk,
and `RESERVATION_CHECK` confirms the correct slot was booked.

## 2. Timing table

Offsets from window open, 06:30:00.000 CT:

| Step | Sent (offset) | Reply / round trip | → next |
|---|---|---|---|
| Job start | −1h52 (06:28:06) | — | +2s |
| Login | 06:28:08.222 | success @ 06:28:23.013 (~14.8s) | 0 |
| Course select (Northgate) | 06:28:23 | verified @ 06:28:37 (~14s) | 0 |
| Date select (calendar) | 06:28:37 | complete @ 06:28:42.702 (~5.7s) | 0 |
| Pre-scroll sheet (154 items) | 06:28:42.702 | loaded @ 06:28:42.790 | 0 |
| Slot scan / pre-locate 12:08 PM (idx 39, exact, 4 fallbacks) | 06:28:43.028 | — | +1.4s |
| Connection-warm HEAD probe | 06:28:44.425 | 200 OK, round trip 782ms | 0 |
| Clock skew probe (111 probes, 5 transitions) | 06:28:44.455–49.488 | measured: club +8ms, one-way 12ms, tick ±23ms | — |
| Lead adjustment | — | fire 20ms early | — |
| **Reserve 1/6 fired** (12:08 PM, burst #0) | **+1010ms (06:30:01.827 logged)** | **accepted, roundTripMs 806, server +1001ms** | |
| Reserve 2 (burst #1) | +1110ms | refused, roundTripMs 548 | |
| Reserve 3 (burst #2) | +1230ms | refused (frozen dup), roundTripMs 518 | |
| Reserve 4 (burst #3) | +1380ms | refused, roundTripMs 845 | |
| Reserve 5 (burst #4) | +1530ms | refused, roundTripMs 682 | |
| Reserve 6 (burst #5) | +1710ms | accepted (surplus hold), roundTripMs 509, server +2001ms | |
| Opening burst done | 2651ms elapsed | 6 sent, 4 refused, 0 errored, granted by member #0 | |
| **RACE_LEDGER / GATE**: granted 12:08 PM | +1010ms (boundary); last refusal +1530ms | club gate open by :01 | |
| Player count (4) | 06:30:03.644 | 200 OK, ~77ms | |
| TBD guest 1 | 06:30:03.738 | 200 OK, ~90ms | |
| TBD guest 2 | 06:30:03.837 | 200 OK, ~74ms | |
| TBD guest 3 | 06:30:03.917 | 200 OK, ~75ms | |
| Book Now | 06:30:04.003 | 200 OK, ~558ms | |
| Chain finished | 06:30:06.139 | phase=complete, success=True, totalMs=3613 | +9.8s |
| **RESERVATION_CHECK** | started 06:30:06.146 | found @ 06:30:15.926, RESERVED confirmed | |
| Post-race sheet saved | 06:30:38.447 | succeeded=1, failed=0 | |

Round trips ranged 509–845ms — comfortably inside the 3.0s
`_RESERVE_TIMEOUT_S` budget (28% of it at the worst), nowhere near the
2935ms outlier from 08-16 that `booking-postmortem` §7b flags as the one
thing to watch. Nothing here moves that watch item.

## 3. Fix needed

None. Clean win on the first rung, no anomalies in the ledger or chain.
The one open thread — whether a same-slot surplus hold (as opposed to
09-04's cross-slot one) ever causes a club-side conflict once its own
timer lapses — isn't contradicted by anything in this run's evidence and
is a low-priority watch item, not an actionable bug.
