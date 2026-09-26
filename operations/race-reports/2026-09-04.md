# Booking post-mortem: 2026-09-04

**Target:** Friday 2026-09-11, 08:38 AM, 4 players, Northgate
**Window:** Friday 2026-09-04 06:30:00 CT (job fires 06:28)
**Outcome: lost.** Fourteen Reserves, every one refused, out to +10,737ms past
the window; `RESERVATION_CHECK: No reservation listed for this tee time`. The
whole 07:53–09:00 block was held by other members on the sheet photographed
44 seconds after the window; 09:08 AM — the last entry on our own fallback
list — was still Available on it.
**Code that ran:** `72e3a3d` (#170), current `main` at run time. The first
Friday race on #166.

**Status: diagnosed, fix in the same PR as this document.** Two things went
wrong, one of them self-inflicted, and neither is the one the first pass of
this post-mortem named. Three earlier readings are withdrawn below.

---

## What the raw data says, in the order it was established

**1. Our first request is identical every morning; only the answer differs.**
Attempt 1 across the eight races in the bucket:

| morning | day | sent | club clock | round trip | verdict |
|---|---|---|---|---|---|
| 08-20 | Thu | +1006ms | :01 | 456ms | accepted |
| 08-21 | Fri | +1015ms | :01 | 465ms | refused |
| 08-27 | Thu | +1023ms | :01 | 675ms | accepted |
| 08-28 | Fri | +1005ms | :01 | 444ms | refused |
| 08-29 | Sat | +1012ms | :01 | 777ms | accepted |
| 09-02 | Wed | +1000ms | :01 | 775ms | accepted |
| 09-03 | Thu | +1013ms | :01 | 523ms | accepted |
| **09-04** | **Fri** | **+1026ms** | **:01** | **474ms** | **refused** |

Same request shape, same offset, same club-second. Nothing we send differs on
a Friday.

**2. Today's fourteen responses were one response.** All 14 were exactly
674,873 bytes; the two the ledger stored (attempts 1 and 14, 9.7s apart) are
byte-identical, MD5 `92d12ce3…`. Attempt 14, fired at +10,737ms, still carried
`Booking Starts In : 00:01:20` — the staged sheet's countdown, captured at
06:28:41. A live render ten seconds past the window cannot say "1:20 to go".
The club was re-rendering the pre-window snapshot, fourteen times.

**3. On the two Fridays we won, the responses changed mid-race.** 08-21:
attempt 1 → 2 changed (670975 → 670775 bytes), attempts 2-4 identical. 08-28:
attempts 1-2 identical (666848), attempt 3 changed (666648, `sheetOpen` flipped
to True), attempts 3-4 identical. Our own requests do not change the view (the
identical consecutive refusals prove it); something external changed it once,
between +1.0s and +2.8s, on both mornings. Today, nothing did.

**4. What changed between 08-28 and today: PR #166.** Merged 2026-08-28 20:51 CT,
the evening *after* the 08-28 race. It clears the parked Chrome page's JS timers
— calling the club's own `stopSheetTimers()` and then every `clearTimeout` /
`clearInterval` handle — when the **first Reserve is answered**. Today that was
+1.6s, on a refusal. The page's own load script is
`$(function(){stopSheetTimers(); executeTimeoutTimer('5');})`; our own code
documents those timers as *"the site's own timer removing the 'disable-div'
overlay at 6:30"*. The three non-Friday races that ran on #166 (08-29, 09-02,
09-03) won on attempt 1 — on 09-03 the sweep fired 8ms *after* the grant had
landed — so the sweep never had a chance to matter until the first morning that
needed a second ask.

**5. The frozen view did not cause the refusals.** On 08-28, attempts 3 and 4
had a *refreshed* view (`sheetOpen=True`) and were still refused; attempt 5,
08:45 AM, was accepted. And the refreshed view on both winning Fridays still
rendered 08:38, 08:30 and 08:45 as **Available** while the club was refusing
them — the only text-level change in 670KB was the countdown disappearing.
The verdict is computed live by the club; the rendered sheet in a refusal is
never live. So today's fourteen "no"s were true answers: 08:38, 08:30, 08:45
and 08:23 really were gone by the time we asked each (all four named on the
postrace sheet).

**6. What the frozen view *did* do: blind the policy.** The hold-until-open
policy re-asks the target while the response renders the sheet closed. With
the view frozen at the pre-window render, `sheetOpen` read `False` fourteen
times. Attempt accounting:

| attempts | slot | why |
|---|---|---|
| 1-9 | 08:38 | hold: "sheet still closed", to the 8000ms cap |
| 10 | 08:30 | first fallback, at +8117ms |
| 11, 13 | 08:38 | target-interleave |
| 12, 14 | 08:45, 08:23 | fallbacks |
| — | 08:53, 08:15, 08:08, **09:08** | never reached; `_RESERVE_DEADLINE_MS` (10s) ended the race at attempt 14 |

Eleven of fourteen asks went to a slot that was gone from the first one. On
08-28, the view refreshed at +2.8s, the hold released, and attempt 5 got 08:45.

**7. Friday's prime block is a stampede; Thursday's is not.** Postrace sheets,
07:00–09:15 rows: Fri 09-04 (+44s) 12 of 17 taken, 07:53–09:00 contiguous.
Thu 09-03 (+28s) 9 of 17 taken, with 08:15/08:23/08:38/08:45 still open.

**8. Our cadence cannot be in that race.** Gaps between our asks today:
703, 801, 692, 1309, 1070ms — one round trip plus ~140ms of parse, serial. Every
Friday's target was gone before the second ask. Whether the deciding instant is
the `:01` tick with a competitor inside 26ms of it, or a gate that opens
somewhere in `:01.3`–`:02.8` with a crowd's retries landing every second, one
ask per 750ms loses it. The artifacts cannot separate those two sub-cases (no
booked-at timestamps are exposed, the club's JS is unreachable from the
post-mortem container, and we never asked a second slot before `:04` on any
Friday) — but the fix is the same under both, and its ledger will settle which.

## Withdrawn

- *"The sheet was closed for 10 seconds."* (first pass of this post-mortem.)
  The sheet-open marker in a refusal describes our own view as of its last
  refresh, not the club. Fourteen refusals read "closed" against a sheet other
  members were booking from.
- *"Friday lateness is increasing week over week."* Over-fit to three points;
  today's +10.7s was a frozen view, not a later gate. There is no trend.
- *"Raise `hold_cap_ms`."* The cap is downstream of a signal that means
  nothing; with the view frozen, any cap loses.

## Fix (#TBD, this PR)

1. **Opening burst** (`walden_reserve_opening_mode = "burst"`, every day): the
   requests in `walden_reserve_burst_offsets_ms` — twelve, from the tick to
   +2.6s — are sent at their instants without waiting for answers, target for
   the first four, then alternating fallback and target. First grant wins;
   members not yet sent when it lands are skipped. The serial fallback walk
   continues after it, twice over the list. The ladder, pair, hold and
   interleave are untouched behind `"ladder"` for the rollback.
2. **Aim on the tick**: `walden_reserve_aim_margin_ms` 30 → 0.
3. **Deadline** `_RESERVE_DEADLINE_MS` 10s → 30s, so the walk reaches the end
   of the list (09:08 was free at +44s).
4. **Quiet the browser on a grant, not on the first answer**, and never call
   the club's `stopSheetTimers()`.
5. **Ledger and logs**: `statusCode`, `responseHeaders` (Retry-After, X-RateLimit-*),
   `errorBody`, `burstIndex`, `bodyDigest`, `identicalToPrevious` on every row; a
   non-200 is `errored`, not a timeout, and closes nothing; `GATE:` lines per
   club-second say what was asked and what was granted, which is the only way
   the club's two identical refusals can be told apart.

**Before a race:** an ad-hoc booking on the burst, to see whether the club
tolerates twelve POSTs on one view in 2.6s, whether a second grant on a
different slot breaks the chain, and whether anything rate-limits. If it
misbehaves: `WALDEN_RESERVE_OPENING_MODE=ladder` on the service.

## Round-trip table (§7b of the skill)

474, 407, 471, 1065, 799, 538, 316, 405, 286, 313, 325, 327, 397, 274ms. Nothing
near the 3.0s timeout; the two over 750ms (attempts 4 and 5) are within the
range 08-16 set.
