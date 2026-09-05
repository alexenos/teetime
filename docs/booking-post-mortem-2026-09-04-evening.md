# Booking post-mortem: 2026-09-04 evening (ad-hoc)

Target booking date: **2026-09-08**, 05:06 PM, 4 players, Walden Northgate.
Not a race morning — an ad-hoc Discord request, run as the explicit pre-race
test the opening burst (PR #173, merged ~19:23 CT the same day) was built to
need: "whether the club tolerates twelve POSTs on one view in 2.6s, whether a
second grant on a different slot breaks the chain" (docs/booking-post-mortem-2026-09-04.md,
"Fix" §, item before a race).

## 1. Verdict: booked the wrong slot, reported success for the right one

The burst got a grant fast — attempt 1, the target, accepted at +1010ms — but
the club's own reservation record ended up anchored to a *different* slot than
the one the chain reported booking.

`RACE_LEDGER` (`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260905_010402/ledger.jsonl`):

| attempt | burstIndex | slot asked | sent | verdict | form returned |
|---|---|---|---|---|---|
| 1 | 0 (target) | 05:06 PM | +1010ms | accepted | 05:06 PM |
| 2 | 1 (target) | 05:06 PM | +1110ms | refused | — |
| 3 | 2 (target) | 05:06 PM | +1231ms | refused | — |
| 4 | 3 (target) | 05:06 PM | +1380ms | refused | — |
| 5 | 4 (fallback) | 04:58 PM | +1530ms | **accepted** | 04:58 PM |
| 6 | 5 (target) | 05:06 PM | +1710ms | **accepted** | 04:58 PM |

Members #4 and #5 were logged by the booker itself as surplus:

```
WARNING - Burst member #5 was also granted 05:06 PM - a surplus hold, left to
the club's hold timer; the ad-hoc test of this mode is what says whether the
club tolerates two
WARNING - Burst member #4 was also granted 04:58 PM - a surplus hold, left to
the club's hold timer; the ad-hoc test of this mode is what says whether the
club tolerates two
```

`Opening burst done ... granted by member #0` — the code adopted the target's
grant (05:06 PM) and proceeded on it: added two TBD guests, clicked Book Now,
and reported `Chain finished - phase=complete, success=True, booked=05:06 PM`.
`RESERVATION_CHECK` read back:

```
TEE TIMES (NORTHGATE) 09/08/2026 04:58 PM - 05:06 PM RESERVED
```

— consistent with either a merged display or an anchor at 04:58 PM. The
post-race tee sheet (`walden/postrace/20260905_010433_for_20260908/tee_sheet.html`)
settles it: slot index 54 (04:58 PM) is rendered `background-color:#ed1717`
with `custom-reserved-slot-div` and a member name; slot index 55 (05:06 PM —
the requested time, the one the chain and the Discord confirmation both named)
is rendered `background-color:#237f40`, an ordinary open `custom-slot-sel-div`
link. **The club finalized the fallback, not the target — and the target slot
was open again by the time the run finished.**

## 2. Mechanism (inferred from the evidence, not directly instrumented)

All six attempts carried the same PrimeFaces `viewState` (`b0b8be68`) —
by design, every burst member replays the same staged conversation, one
socket write each. That is fine when only one grant lands. Here the target
(#0) and two others (#4, #5) were all in flight before the "skip after grant"
check could stop them, and two of the three landed on 04:58 PM. Since every
member mutates the *same* server-side view rather than an independent one,
whichever grant the server processed last is what the view held pending by
the time "Add TBD guest" / "Book Now" ran against it — and that appears to
have been a 04:58 PM grant, arriving after #0's, even though the client-side
code (correctly, by its own bookkeeping) had already adopted #0 as the win.
The comment this contradicts is at `walden_http_booker.py:1917`
("a surplus hold ... left to the club's own hold timer") — the surplus here
was not left inert; it became the reservation of record.

This is not a "4 players spans two 8-minute rows" artifact of the club's UI:
two prior single-attempt, no-burst race mornings (2026-08-20, 2026-09-03) also
booked 4 players and also display as an `X - Y` range on the reservations
page, but in both of those the range starts *at* the granted time and ends
~7 minutes later (`08:08 AM - 08:15 AM`), and the post-race sheet for those
mornings has never been checked row-by-row against it. Here the range *ends*
at the requested time and starts one slot earlier — the opposite order — and
the row-level sheet evidence above independently confirms which end is the
one actually reserved. Whether the display format is normally
`[granted, +7min]` and this run is genuinely anchored one row early, or
something else, is not fully settled; what is settled, from the sheet's own
`custom-reserved-slot-div` class, is which row the club currently holds for
this member.

## 3. Not at risk this run

- Timing budget: round trips 189–684ms, nowhere near the 3.0s
  `_RESERVE_TIMEOUT_S` (see skill §7b).
- No throttling: `errored=0` across all six attempts.
- No duplicate charge / two separate reservations: one reservation row exists,
  not two.

## 4. Fix (this PR)

`walden_reserve_burst_target_only` now defaults to the full burst plan (12),
so by default **no fallback is interleaved into the burst** — every member
asks for the requested slot alone. The fallback list is walked serially
afterward, unchanged: one request in flight at a time, waiting for each
answer, exactly the path already taken when nothing inside the burst was
granted. This removes the condition that produced the mixup (two different
slots granted under one shared ViewState) without touching the burst's actual
purpose — landing the *target* ask early and often, which is unaffected by
removing the fallback interleave. The alternating-fallback code itself is
untouched and stays reachable via `WALDEN_RESERVE_BURST_TARGET_ONLY` below the
offset count, for whenever concurrent multi-slot grants under one ViewState
are made safe.

**Not yet done:** the underlying session-sharing issue (concurrent Reserve
calls against one ViewState racing each other server-side) is still there in
principle for the four now-serial target-only retries within the burst itself
(members 0–3 all ask the *same* slot, so a mixup between them is harmless —
worst case is redundant refusals), but has not been proven safe for any future
mode that reintroduces concurrent *different*-slot asks. Before
`WALDEN_RESERVE_BURST_TARGET_ONLY` is ever lowered again, that needs its own
ad-hoc test.

## Round-trip table (§7b of the skill)

684, 613, 655, 645, 389, 189ms. Nothing near the 3.0s timeout.
