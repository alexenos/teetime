# Booking post-mortem: 2026-09-10

Target booking date: **2026-09-17** (Thursday), 08:08 AM, 4 players, Walden Northgate.

## 1. Verdict: WON, first rung

The club granted 08:08 AM to burst member #0, sent at +1010ms past the
window. `RESERVATION_CHECK` independently confirmed it on the member's
reservations page:

```
TEE TIMES (NORTHGATE ) 09/17/2026 08:08 AM - 08:15 AM 09/17/2026 08:08 AM - 08:15 AM RESERVED
```

The winning row's server script is
`executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;`
— the accepted-verdict signature. The opening burst had 12 target-only
asks staged; 7 were actually sent before the remaining 5 were skipped
after the grant landed.

`GATE:` lines place the boundary at club-clock second `:02`: the 4 asks
that arrived in `:01` were all refused, the 3 that arrived in `:02`
(burst members #0, #5, #6) were all granted. Member #0 — sent earliest —
happened to land in `:02` on round-trip timing and won; #5 and #6 were
surplus holds for the same slot, left to the club's hold timer, per the
booker's own log line. No cross-slot risk, since PR #174 restricts the
burst to target-only asks.

Race ledger
(`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260910_113005/ledger.jsonl`):

| attempt | burstIndex | slot | sent | verdict | roundTripMs | server clock |
|---|---|---|---|---|---|---|
| 1 | 0 | 08:08 AM | +1010ms | **accepted** | 1106 | +2001ms |
| 2 | 1 | 08:08 AM | +1110ms | refused | 619 | +1001ms |
| 3 | 2 | 08:08 AM | +1230ms | refused | 514 | +1001ms |
| 4 | 3 | 08:08 AM | +1380ms | refused (frozen dup) | 450 | +1001ms |
| 5 | 4 | 08:08 AM | +1530ms | refused (frozen dup) | 346 | +1001ms |
| 6 | 5 | 08:08 AM | +1715ms | accepted (surplus hold) | 618 | +2001ms |
| 7 | 6 | 08:08 AM | +1919ms | accepted (surplus hold) | 329 | +2001ms |

## 2. Timing table

Offsets from window open, 06:30:00.000 CT:

| Step | Sent (offset) | Reply / round trip | → next |
|---|---|---|---|
| Job start | 06:28:04.855 | window opens 06:30:00, batch of 1 | +7.9s |
| Login | 06:28:12.758 | success @ 06:28:26.528 (~13.8s) | 0 |
| Tee time page | 06:28:26.528 | loaded @ 06:28:31.221 (4.7s) | 0 |
| Course select (Northgate) | 06:28:31.221 | verified @ 06:28:34.749 (3.5s) | 0 |
| Date select (calendar) | 06:28:34.749 | complete @ 06:28:39.154 (4.4s) | 0 |
| Pre-scroll sheet (143 items) | 06:28:39.154 | loaded @ 06:28:39.222 (68ms) | 0 |
| Slot scan / pre-locate 08:08 AM (idx 7, exact, 8 fallbacks; dropped course=64, window=70) | 06:28:39.368 | — | +0.3s |
| Adopt browser session (direct HTTP) | 06:28:39.680 | 8 cookies, 12 fields, ViewState present | +0.3s |
| Reserve staged (burst of 12) | 06:28:40.024 | 1832 body bytes | +0.8s |
| Connection-warm HEAD probe | 06:28:40.870 | 200 OK, round trip 847ms | 0 |
| Clock skew probe (105 probes, 5 transitions) | 06:28:41.038–46.049 | club +7ms, one-way 14ms, tick ±24ms | — |
| Lead adjustment | — | fire 20ms early | — |
| **Reserve 1/30 fired** (08:08 AM, burst #0) | **+1010ms (06:30:00.959 logged, 960ms past window)** | **accepted, roundTripMs 1106, server +2001ms** | |
| Reserve 2 (burst #1) | +1110ms | refused, roundTripMs 619 | |
| Reserve 3 (burst #2) | +1230ms | refused, roundTripMs 514 | |
| Reserve 4 (burst #3) | +1380ms | refused (frozen dup), roundTripMs 450 | |
| Reserve 5 (burst #4) | +1530ms | refused (frozen dup), roundTripMs 346 | |
| Reserve 6 (burst #5) | +1715ms | accepted (surplus hold), roundTripMs 618 | |
| Reserve 7 (burst #6) | +1919ms | accepted (surplus hold), roundTripMs 329 | |
| Opening burst done | 2651ms elapsed | 7 sent, 5 skipped after grant, 4 refused, 0 errored, granted by #0 | |
| **RACE_LEDGER / GATE**: granted 08:08 AM | +1010ms (boundary); last refusal +1530ms | club gate open by `:02` | |
| Player count (4) | 06:30:03.655 | 200 OK, ~83ms | |
| TBD guest 1 | 06:30:03.757 | 200 OK, ~84ms | |
| TBD guest 2 | 06:30:03.850 | 200 OK, ~85ms | |
| TBD guest 3 | 06:30:03.944 | 200 OK, ~87ms | |
| Book Now | 06:30:04.042 | 200 OK, ~660ms | |
| Chain finished | 06:30:06.481 | phase=complete, success=True, blocked=False, totalMs=3756 | +0.6s |
| **RESERVATION_CHECK** | started 06:30:06.487 | found @ 06:30:14.030, RESERVED confirmed | |
| Post-race sheet saved | 06:30:29.951 | succeeded=1, failed=0 | |

Round trips ranged 329–1106ms — comfortably inside the 3.0s
`_RESERVE_TIMEOUT_S` budget (37% of it at the worst), nowhere near the
2935ms outlier from 08-16 that `booking-postmortem` §7b flags as the one
thing to watch. Nothing here moves that watch item.

## 3. Fix needed

None. Clean win on the first rung, no anomalies in the ledger or chain.
The granting response for member #0 also carried the stale
"blocked by another user" text alongside the accept, which the booker
correctly treated as stale and ignored for the rest of the chain — the
same benign artifact §5 of `booking-postmortem` describes, not a new
finding. Two surplus holds (#5, #6) landed for the same slot after the
win, per PR #174's target-only burst design — no cross-slot risk, and
`RESERVATION_CHECK` confirms the correct slot was booked.
