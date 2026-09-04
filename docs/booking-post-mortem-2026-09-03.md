# Booking post-mortem: 2026-09-03

Target booking date: **2026-09-10**, 08:08 AM, 4 players, Walden Northgate.

## 1. Verdict: WON, first rung

The club granted 08:08 AM on attempt 1/8, sent at +1013ms past the window,
club server-side at +1001ms. `RESERVATION_CHECK` independently confirmed it
on the member's reservations page:

```
TEE TIMES (NORTHGATE) 09/10/2026 08:08 AM - 08:15 AM RESERVED
```

`evalText` from the race ledger is
`executeHoldTimeTimer('300');;stopSheetTimers();;...` with no `.show()`
call — the accepted-verdict signature, not the refused one. `popupPresent:
false`. Only one Reserve was ever fired; no fallback slots were needed.

Race ledger row (`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260903_113002/ledger.jsonl`):

```json
{"attempt": 1, "slot": "08:08 AM", "verdict": "accepted", "reason": "club returned its booking form for 08:08 AM", "sentMsPastWindow": 1013, "roundTripMs": 523, "serverDateMs": 1788435001000, "serverMsPastWindow": 1001, "responseBytes": 84066, "sheetRows": 0, "reserveButtons": 0, "reservationFormSlot": "08:08 AM", "popupPresent": false, "evalText": "executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;"}
```

## 2. Timing table

Offsets from window open, 06:30:00.000 CT:

| Step | Sent (offset) | Reply / round trip | → next |
|---|---|---|---|
| Job start | −1h52 (06:28:07) | — | +11s |
| Login | 06:28:18 | success @ 06:28:34 (~8.1s) | +0.1s |
| Nav to tee-time page | 06:28:34 | loaded @ 06:28:39 (~5.1s) | 0 |
| Course select (Northgate) | 06:28:39 | verified @ 06:28:44 (~4.5s) | 0 |
| Date select (calendar) | 06:28:44 | complete @ 06:28:48 (~4.2s) | 0 |
| Pre-scroll sheet (143 items) | 06:28:48 | — | 0 |
| Slot scan / pre-locate 08:08 AM (idx 7, exact) | 06:28:49 | — | +2s |
| Connection-warm HEAD probe | 06:28:51 | 200 OK, round trip 745ms | 0 |
| Clock skew probe (111 probes, 5 transitions) | 06:28:51–56 | measured: club +4ms, one-way 12ms, tick ±23ms | — |
| Lead adjustment | — | fire 17ms early | — |
| **Reserve 1/8 fired** (08:08 AM) | **+1013ms (06:30:01.012)** | **accepted, roundTripMs 523, server +1001ms** | +44ms |
| Player count (4) | +1057ms | 200 OK, ~78ms | +24ms |
| TBD guest 1 | +1148ms | 200 OK, ~80ms | +10ms |
| TBD guest 2 | +1238ms | 200 OK, ~79ms | +15ms |
| TBD guest 3 | +1318ms | 200 OK, ~79ms | +15ms |
| Book Now | +1412ms | 200 OK, ~851ms | — |
| **RACE_LEDGER**: granted 08:08 AM | +1013ms (boundary) | — | — |
| Chain finished | phase=complete, success=True, totalMs=1850 | — | +9.4s |
| **RESERVATION_CHECK** | — | found @ 06:30:12.629, RESERVED confirmed | — |
| Batch complete | 06:30:41 (succeeded=1, failed=0) | — | — |

Round trip on the winning Reserve was 523ms — comfortably inside the 3.0s
`_RESERVE_TIMEOUT_S` budget (17% of it), and well below the 2935ms outlier
from 08-16 that `booking-postmortem` §7b flags as the one thing to watch.
Nothing here moves that watch item.

## 3. Fix needed

None. Clean win on the first rung, no anomalies in the ledger, no
popup/refusal artifacts to interpret, verification confirmed independently.
