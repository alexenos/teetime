# Booking post-mortem: 2026-09-06

Target booking date: **2026-09-13** (Sunday), 08:00 AM, 4 players, Walden Northgate.

## 1. Verdict: WON, first ask — but 226ms from throwing it away

The club granted 08:00 AM to burst member #0, the very first Reserve, sent at
+994ms past the window. `RESERVATION_CHECK` independently confirmed it:

```
TEE TIMES (NORTHGATE ) 09/13/2026 08:00 AM - 08:08 AM RESERVED
```

The winning row's `evalText` is
`executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;`
— the accepted-verdict signature. The burst sent 12 target-only asks (PR #174);
member #11 was also granted the same slot and was left to the club's hold timer
as a surplus hold, the same same-slot case as 09-05 and not a cross-slot risk.

The morning's real finding is the winning ask's **`roundTripMs` of 2774** against
a 3.0s `_RESERVE_TIMEOUT_S`: 226ms of margin on the request that won the tee
time. See §3 — the club had not finished answering it, and a timeout there would
not merely have lost the slot, it would have latched `timed_out` and reported the
run as unknown.

Race ledger
(`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260906_113008/ledger.jsonl`):

| attempt | burstIndex | sent | verdict | roundTripMs | server clock | bytes |
|---|---|---|---|---|---|---|
| 1 | 0 | +994ms | **accepted** | **2774** | **+3001ms** | 86136 |
| 2 | 1 | +1094ms | refused | 527 | +1001ms | 588428 |
| 3 | 2 | +1214ms | refused | 472 | +1001ms | 588428 |
| 4 | 3 | +1364ms | refused | 417 | +1001ms | 588428 |
| 5 | 4 | +1514ms | refused | 420 | +1001ms | 588228 |
| 6 | 5 | +1694ms | refused | 253 | +1001ms | 588228 |
| 7 | 6 | +1894ms | refused | 343 | +2001ms | 588228 |
| 8 | 7 | +2144ms | refused | 293 | +2001ms | 588228 |
| 9 | 8 | +2444ms | refused | 391 | +2001ms | 588228 |
| 10 | 9 | +2794ms | refused | 576 | +3001ms | 588228 |
| 11 | 10 | +3194ms | refused | 333 | +3001ms | 588228 |
| 12 | 11 | +3594ms | accepted (surplus hold) | 216 | +3001ms | 86136 |

All 12 asks were for 08:00 AM, so per §7d's rule — a single slot asked repeatedly
proves nothing about gate-versus-taken — the `GATE:` lines cannot separate the
two readings on their own. What they do say: `club :01` 5 asks granted none,
`club :02` 3 asks granted none, `club :03` 4 asks **GRANTED**.

## 2. Timing table

Offsets from the stated window, 06:30:00.000 CT.

| Step | Sent (offset) | Reply / round trip | → next |
|---|---|---|---|
| Job start | 06:28:05.133 | batch of 1, window 06:30:00 | +9.4s |
| Login | 06:28:14.567 | success @ 06:28:28.078 (~13.5s) | 0 |
| Tee time page | 06:28:28.078 | loaded @ 06:28:34.638 (6.6s) | 0 |
| Course select (Northgate) | 06:28:34.638 | verified @ 06:28:40.534 (5.9s) | 0 |
| Date select (calendar) | 06:28:40.534 | complete @ 06:28:45.464 (4.9s) | 0 |
| Pre-scroll sheet (153 items) | 06:28:45.474 | loaded @ 06:28:45.555 (81ms) | 0 |
| Slot scan / pre-locate 08:00 AM (idx 6, exact, 8 fallbacks; dropped course=67, window=77) | 06:28:45.734 | — | +0.6s |
| Adopt browser session (direct HTTP) | 06:28:46.337 | 8 cookies, 12 fields, ViewState `24588a2c` | +0.3s |
| Reserve staged (burst of 12) | 06:28:46.649 | 1829 body bytes | +0.7s |
| Connection-warm HEAD probe | 06:28:47.383 | 200 OK, round trip **734ms** | 0 |
| Clock skew probe (101 probes, 5 transitions) | 06:28:47.415–52.418 | club +22ms, one-way 14ms, tick ±25ms | — |
| Lead adjustment | — | fire 36ms early | — |
| **Reserve 1/30 fired** (burst #0) | **+994ms** | **accepted, roundTripMs 2774, server +3001ms** | ← **the 226ms near-miss** |
| Reserve 2 (burst #1) | +1094ms | refused, roundTripMs 527, server +1001ms | |
| Reserve 3 (burst #2) | +1214ms | refused, roundTripMs 472 | |
| Reserve 4 (burst #3) | +1364ms | refused, roundTripMs 417 | |
| Reserve 5 (burst #4) | +1514ms | refused, roundTripMs 420 | |
| Reserve 6 (burst #5) | +1694ms | refused, roundTripMs 253 | |
| Reserve 7 (burst #6) | +1894ms | refused, roundTripMs 343, server +2001ms | |
| Reserve 8 (burst #7) | +2144ms | refused, roundTripMs 293 | |
| Reserve 9 (burst #8) | +2444ms | refused, roundTripMs 391 | |
| Reserve 10 (burst #9) | +2794ms | refused, roundTripMs 576, server +3001ms | |
| Reserve 11 (burst #10) | +3194ms | refused, roundTripMs 333 | |
| Reserve 12 (burst #11) | +3594ms | accepted (surplus hold), roundTripMs 216 | |
| Opening burst done | 5013ms elapsed | 12 sent, 0 skipped, 10 refused, 0 errored, granted by #0 | |
| **RACE_LEDGER / GATE** | granted +994ms; last refusal +3194ms | `GATE:` line reads "gate was open by club :03" — its own wording, and see §3b on why a club-second cannot carry that | |
| Player count (4) | 06:30:06.039 | 200 OK, ~78ms | |
| TBD guest 1 | 06:30:06.135 | 200 OK, ~79ms | |
| TBD guest 2 | 06:30:06.222 | 200 OK, ~78ms | |
| TBD guest 3 | 06:30:06.308 | 200 OK, ~79ms | |
| Book Now | 06:30:06.401 | 200 OK, ~686ms | |
| Chain finished | 06:30:08.990 | phase=complete, success=True, totalMs=6157 | +0.9s |
| **RESERVATION_CHECK** | started 06:30:09.910 | found @ 06:30:17.960, RESERVED confirmed | |
| Post-race sheet saved | 06:30:37.892 | succeeded=1, failed=0 | |

Nothing broke. The table is the full chain of a clean win; the one row worth
reading twice is the first Reserve's 2774ms.

## 3. Why the winning round trip was 2774ms

**Unresolved.** The delay is on the club's side, and it is the ask that won — but
the mechanism is not established, and an earlier draft of this document claimed
it was. That claim is withdrawn; §3b says why, because the mistake is an easy one
to make again.

### 3a. What the artifacts do establish

- **Not our container.** Attempt 1's `postResponseWallMs` 23 / `postResponseCpuMs`
  20 — and that segment falls *after* the round trip anyway. No §7c descheduling.
- **Not the network, and not load we can see.** The other eleven members, same
  client and same instant, answered in 253–576ms.
- **Not the cost of granting.** Member #11 performed the identical grant — same
  slot, same 86136-byte booking form — in **216ms**, 42ms after #0's answer landed.
- **Not self-contention from our own burst.** 08-16 fired exactly **one** Reserve,
  with no siblings to block on, and still took 2935ms.
- **The verdict tracks when the club answered, not when we sent.** Ordered by
  answer time, 09-06 splits cleanly — and note #0 was sent *first* and #11 *last*:

  | answered | sent | verdict |
  |---|---|---|
  | +1621 … +3527ms (ten asks) | +1094 … +3194ms | refused |
  | **+3768ms** | +994ms (#0) | **accepted** |
  | **+3810ms** | +3594ms (#11) | **accepted** |

  Something on the club's side changed between +3527ms and +3768ms. That is the
  one non-arithmetic signal this morning carries.

### 3b. Withdrawn: the club-second table

An earlier draft argued from the nine races on record that answers stamped
club-second `:01` came back in 456–831ms while the only two ~2.8s mornings
(08-16, 09-06) were stamped `:03`, and concluded that the club parks a pre-gate
ask and answers it when the gate flips.

**That table is a tautology.** `serverMsPastWindow` is derived from the HTTP
`Date` header, which is **whole-second resolution** (§7a), so it only ever reads
1001 / 2001 / 3001 — second-buckets, not timestamps. It restates
`sent + roundTripMs` truncated to the second, and all twelve of this morning's
rows match that arithmetic exactly, with no residual:

| # | sent | roundTripMs | sent+RTT | club second |
|---|---|---|---|---|
| 1 | 994 | 2774 | 3768 | 3001 |
| 2 | 1094 | 527 | 1621 | 1001 |
| 7 | 1894 | 343 | 2237 | 2001 |
| 11 | 3194 | 333 | 3527 | 3001 |
| 12 | 3594 | 216 | 3810 | 3001 |

A slow round trip lands in a later club-second **by construction**, whatever the
club is doing. So "answered at `:03`" is not independent evidence of anything, and
nothing about a gate follows from it. Nor were the burst's asks "held to a common
instant": the twelve answers arrived spread across +1621ms to +3810ms, and only
the last four happened to fall inside the `:03` second.

### 3c. The candidate readings, none settled

1. **A gate opening later than the aim assumes** (~+3.6s here, vs the +1.0s the
   aim is built around).
2. **A slow first booking transaction on the club's side** — cold path, or the
   thundering herd of every member's browser firing at the window. This fits both
   slow mornings being the morning's *first* ask, and needs no gate at all.
3. **A hold taken by our own in-flight ask**, with the ten refusals being our own
   #0 mid-transaction. Weakened by the club granting the same slot again 42ms
   later (a surplus hold), and it cannot explain 08-16's single Reserve.

Per §7d the discriminator would be a grant for a *different* slot in the same
club-second, and PR #174's target-only burst means all 12 asks were 08:00 AM — so
this morning cannot separate them. Worth a deeper pass if it recurs; the fix below
does not wait on it.

## 4. Fix: split the opening budget from the walk's

`_RESERVE_TIMEOUT_S = 3.0` was sized (see its comment) on the premise that a long
round trip is *a stalled request*, and that "every second spent waiting on a
stalled request is a second of ladder not walked". That premise is true of the
serial fallback walk. It is false of the opening burst, for two reasons:

1. Twice now the slowest answer of the morning has been the one carrying the
   grant, so a long wait there is not a request worth abandoning — whatever the
   club is doing to produce it (§3c).
2. Burst members are each sent on their own thread (`ThreadPoolExecutor`), so a
   slow member costs no ladder time at all — on this morning member #0 waited
   2774ms while all eleven siblings fired and were answered on schedule.

Both reasons are independent of §3's unresolved mechanism, which is why the fix
does not wait on settling it.

Meanwhile the downside is severe and asymmetric. A timeout does not just lose the
ask: it latches `timed_out`, which closes the fallback list for the rest of the
run and sets `result.blocked = not timed_out`, so the run reports as *unknown*
rather than booked — §6's "success=True / no reservation" class, thrown at the
exact moment the club was about to grant. Margins on record: **226ms** (09-06)
and **65ms** (08-16). Two of nine mornings came within a quarter-second of it.

The change:

- New `_RESERVE_OPENING_TIMEOUT_S = 10.0`, used by the opening burst and the
  opening pair. ~3.4x the slowest round trip ever recorded, and well inside
  `_RESERVE_DEADLINE_MS` (30s).
- `_RESERVE_TIMEOUT_S` stays **3.0s** for the serial walk, where the trade that
  sized it is still real.
- `_failed_observation` takes the budget actually spent, so a timed-out burst row
  reports 10000ms rather than claiming 3000ms — a row understating the wait by
  seven seconds is what a later post-mortem would reason from.

Not changed, deliberately: **the aim stays at +1030ms.** Seven of the nine races
were won by an ask sent at ~+1.0s and answered in under a second, and moving the
aim later would forfeit those. Whether firing *before* the club is ready is also
an advantage depends on §3's unresolved mechanism, so it is not claimed here as a
reason.
