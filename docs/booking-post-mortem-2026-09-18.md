# Booking post-mortem: 2026-09-18

**Targets:** Friday 2026-09-25, Northgate, 4 players each — two bookings, two
requesters, racing the same window as separate `teetime-racer` job tasks:

| booking | target | outcome |
|---|---|---|
| task 3 (`6bcb7231`) | 08:38 AM | **booked** 08:38 AM |
| task 1 (`dd2542c7`) | 09:23 AM | **booked** 09:23 AM |

**Window:** Friday 2026-09-18 06:30:00 CT (job fires 06:28)
**Code that ran:** `a804a89` — "Queue ad-hoc bookings behind one browser, and
stop losing their results (#211)", committed 2026-09-17 21:18 CT, `main` at run
time. #205 (task-scoped race ledger directories) and #210 (observer starts
500ms before the window) are both in and both visibly working: the race
artifacts landed under `..._teetime-racer-8nsnr_1` / `..._3`, and the observer's
first snapshot is `snapshot_+-500ms.html`.

**Status: diagnosed, both won.** Both bookings were granted on Reserve #1 and
confirmed by `RESERVATION_CHECK`. Three things worth recording beyond the win:

1. The winning Reserve on 08:38 AM came back in **2888ms**, 112ms inside the
   3.0s `_RESERVE_TIMEOUT_S`. That is the **second** morning on record near
   that budget (2026-08-16, 2935ms), which is the trigger §7b of the
   `booking-postmortem` skill set for raising it.
2. **This is the first Friday on record where the requested slot itself was
   won.** 08:38 AM is the same slot the bot was refused twelve times for on
   2026-09-11, and the post-race sheets identify the party who held it that day
   as the same one now sitting on 08:23 AM.
3. The observer's `disabled` state **conflates two different realities**, and
   this morning's ledger calibrates it for the first time. This weakens the
   Model L / Model F discrimination §7f was built to provide.

---

## 1. Both races, ledger by ledger

Both tasks logged in, navigated, staged and raced independently and
concurrently in one job execution. Both fired an opening burst of 12
target-only asks (`walden_reserve_opening_mode=burst`), and both were granted
on the first.

Note for anyone reading the `GATE:` lines below: `serverMsPastWindow` stamps
the club's **answer**, not its receipt (§7a). With round trips of 1.9s and
2.9s on the two winning asks, the club's receipt of each is only bounded to
`[sent, sent+roundTripMs]`, so the `GATE:` bucketing by club-second attributes
those grants to later seconds than they were sent in. See §5.

### Task 3 — 08:38 AM, booking `6bcb7231`

Slot scan: 151 rows, 9 candidates (dropped `course=64, window=78`), exact match
at index 11, 8 fallbacks behind it (7 off-grid). Clock skew: club +2ms from us,
one-way 13ms, 108 probes, 5 transitions, tick pinned to ±23ms; Reserve fires
16ms early.

| attempt | sent+ms | verdict | roundTripMs | club clock | receipt bound |
|---|---|---|---|---|---|
| 1 | +1014 | **accepted** | **2888** | :03 | [1014, 3902] |
| 2 | +1114 | refused | 537 | :01 | [1114, 1651] |
| 3 | +1234 | refused | 754 | :01 | [1234, 1988] |
| 4 | +1384 | refused | 954 | :02 | [1384, 2338] |
| 5 | +1534 | refused | 918 | :02 | [1534, 2452] |
| 6 | +1714 | refused | 1072 | :02 | [1714, 2786] |
| 7 | +1914 | refused | 831 | :02 | [1914, 2745] |
| 8 | +2164 | refused | 665 | :02 | [2164, 2829] |
| 9 | +2464 | refused | 1126 | :03 | [2464, 3590] |
| 10 | +2814 | refused | 771 | :03 | [2814, 3585] |
| 11 | +3214 | refused | 836 | :03 | [3214, 4050] |
| 12 | +3614 | accepted (surplus hold) | 726 | :04 | [3614, 4340] |

`RACE_LEDGER: club granted 08:38 AM at +1014ms past the window; last refusal
was +3214ms`. Burst done in 3877ms — 12 sent, 0 skipped, 10 refused, 0 errored,
0 never answered. Chain finished `phase=complete, success=True, blocked=False,
clickDrift=0ms, lead=16ms, totalMs=6000`. `RESERVATION_CHECK` confirmed
`TEE TIMES (NORTHGATE ) 09/25/2026 08:38 AM - 08:45 AM ... RESERVED`.

### Task 1 — 09:23 AM, booking `dd2542c7`

Slot scan: 151 rows, 8 candidates (dropped `capacity=1, course=64, window=78`),
exact match at index 17, 7 fallbacks behind it (6 off-grid). Clock skew: club
−0ms from us, one-way 13ms, 108 probes, 5 transitions, tick pinned to ±23ms;
Reserve fires 13ms early.

| attempt | sent+ms | verdict | roundTripMs | club clock | receipt bound |
|---|---|---|---|---|---|
| 1 | +1017 | **accepted** | 1948 | :02 | [1017, 2965] |
| 2 | +1117 | refused | 488 | :01 | [1117, 1605] |
| 3 | +1237 | refused | 567 | :01 | [1237, 1804] |
| 4 | +1387 | refused | 1379 | :02 | [1387, 2766] |
| 5 | +1537 | refused | 1233 | :02 | [1537, 2770] |
| 6 | +1717 | refused | 1044 | :02 | [1717, 2761] |
| 7 | +1917 | refused | 814 | :02 | [1917, 2731] |
| 8 | +2167 | refused | 654 | :02 | [2167, 2821] |
| 9 | +2467 | accepted (surplus hold) | 1030 | :03 | [2467, 3497] |
| 10 | +2817 | accepted (surplus hold) | 678 | :03 | [2817, 3495] |

`RACE_LEDGER: club granted 09:23 AM at +1017ms past the window; last refusal
was +2167ms`. Burst done in 2732ms — 10 sent, 2 skipped after the grant, 7
refused, 0 errored, 0 never answered. Chain finished `phase=complete,
success=True, blocked=False, clickDrift=0ms, lead=13ms, totalMs=5771`.
`RESERVATION_CHECK` confirmed `TEE TIMES (NORTHGATE ) 09/25/2026 09:23 AM -
09:30 AM ... RESERVED`.

Both winning responses carried the accept `<eval>`
(`executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement(...)`), and
every refusal carried `PF('teeSheetValidationErrorPopupVar').show();;` — the
club's real refusal path, as on every morning on record.

## 2. Timing table

Offsets from window open, 06:30:00.000 CT; wall-clock times are UTC.

| Step | Task 3 (08:38 AM) | Task 1 (09:23 AM) |
|---|---|---|
| Observer ready (shared) | 11:27:24.722 — parked on 2026-09-25, 1084 Northgate rows | — |
| Task claimed | 11:29:30.736 | 11:29:30.823 |
| Racer ready (late, see §6) | 90.7s after 06:28:00 CT | 90.8s after 06:28:00 CT |
| Login (Step 1) | 11:29:37.928 → success 11:29:44.425 | 11:29:37.908 → success 11:29:44.343 |
| Tee sheet loaded (Step 2) | 11:29:47.711 | 11:29:47.434 |
| Course verified (Step 3) | 11:29:50.299 | 11:29:49.588 |
| Date selected (Step 4) | 11:29:53.959 | 11:29:53.268 |
| Pre-scroll (Step 5) | 151 items loaded, 11:29:53.989 | 151 items loaded, 11:29:53.306 |
| Slot scan / pre-locate (Step 6) | 11:29:54.046 — idx 11, exact, 8 fallbacks | 11:29:53.382 — idx 17, exact, 7 fallbacks |
| Reserve staged | 11:29:54.716, viewState `5a6a1694` | 11:29:54.110, viewState `8ca69b74` |
| Clock skew measured | club +2ms, one-way 13ms, ±23ms | club −0ms, one-way 13ms, ±23ms |
| Lead | fires 16ms early | fires 13ms early |
| Reserve 1 fired | +964ms (11:30:00.963) | +967ms (11:30:00.966) |
| **Reserve 1 verdict** | **accepted @ +1014ms, RT 2888ms** ⚠ | **accepted @ +1017ms, RT 1948ms** |
| Burst complete | 3877ms, granted by member #0 | 2732ms, granted by member #0 |
| RACE_LEDGER boundary | granted +1014ms; last refusal +3214ms | granted +1017ms; last refusal +2167ms |
| Player count (4) | 11:30:04.900 | 11:30:03.742 |
| TBD guests 1–3 | 11:30:04.987 → 11:30:05.239 | 11:30:03.841 → 11:30:04.100 |
| Book Now clicked | 11:30:05.338 | 11:30:04.112 |
| Chain finished | 11:30:08.410, totalMs=6000 | 11:30:08.174, totalMs=5771 |
| RESERVATION_CHECK confirmed | 11:30:13.005 (~+13.0s) | 11:30:12.680 (~+12.7s) |
| Postrace sheet | **403, lost — see §6** | saved 11:30:28.479 |
| BATCH_JOB complete | 11:30:29.312 | 11:30:29.089 |

Nothing broke. The single point of risk in the whole table is Reserve 1's
2888ms round trip on task 3.

## 3. The observer's `disabled` state conflates two realities

This morning is the first time the observer's snapshots can be calibrated
against grants whose exact times are known from the ledger, and the result
corrects how §7f's flip table should be read.

Our two winning rows, and the rival's row, all trace **identically**:

| offset | row 11 (08:38, ours) | row 17 (09:23, ours) | row 9 (08:23, rival) |
|---|---|---|---|
| −500ms | `empty` | `empty` | `empty` |
| +715ms | `disabled`, no holder | `disabled`, no holder | `disabled`, no holder |
| +4377ms | `disabled`, no holder | `disabled`, no holder | `disabled`, no holder |
| +6903ms | `reserved` — Garner, Ron + 3 TBD | `reserved` — Garner, Melissa + 3 TBD | `reserved` — Rival A, open=3 |
| +7999ms → +13979ms | `reserved`, named | `reserved`, named | `reserved`/`disabled`, flapping |

All 9 snapshots are `refreshOk: true`, `northgateRowCount: 1084`, so none of
this is the staleness trap of §7f's last-but-one paragraph.

We know from the ledger that **we did not hold 08:38 until +1014ms**. So:

- The `disabled` read at **+715ms** is *before* our grant. It cannot be a
  holding — it is the row not being reservable yet.
- The `disabled` read at **+4377ms** is *after* our grant. It is our own hold,
  rendering without a name.

Those are two different realities behind one state string. Therefore **a
`disabled` read can never be used to argue the gate was shut**, which is
precisely the inference §7f promised the observer would support.

Second calibration: the holder's name **lags the grant by ~5.9s** (granted
+1014ms, named +6903ms). That lag is the club's rendering, not the observer's.
So the only unambiguous observer signal is the `reserved` + named-holder
transition, and it arrives ~6s late.

**Trap this created in the first-pass report.** The flip table prints the
holders as of the flip instant, which for our own won slots read
`(-500, +715]ms  disabled  0 named + 0 TBD`. Read at face value that looks
like "an unnamed party took our slot at the window". It was us, three snapshots
later. The flip interval is an upper bound on when a slot stopped being
*available* and says nothing about *who* took it or *when they took it*.

## 4. The near-miss, and the fix it triggers

Task 3's winning Reserve came back in **2888ms** against a 3.0s
`_RESERVE_TIMEOUT_S` — 112ms of margin. Round trips on record, extending §7b's
table:

| morning | kind | roundTripMs |
|---|---|---|
| 08-13 | race | 593 |
| 08-14 | race | 647 |
| 08-14 | ad-hoc | 956 |
| 08-15 | race | 741 / 754 |
| 08-15 | ad-hoc | 942 |
| **08-16** | **race** | **2935** |
| 08-18 | ad-hoc | 522 |
| 08-20 | race | 456 |
| 09-15 | race | 918 / 1230 |
| **09-18** | **race** | **2888** (+ 488–1379 on the other 20 asks) |

§7b set the condition explicitly: *"a second morning near the 3.0s
`_RESERVE_TIMEOUT_S` is the trigger to raise it."* This is that second morning,
and unlike 08-16 it is not a lone outlier in a sample of eight — it is the same
shape (a slow **winning** ask, while every other ask that morning returned in
under 1.4s), which points at server-side latency on the ask that actually
performs the hold rather than at the network.

Had it crossed 3.0s, the run would have abandoned a Reserve the club had
already granted, and per the ladder's own rule a timeout closes the fallback
list for the rest of the run — i.e. it would have produced the
`success=True` / no-reservation class of §6, the one that reads like a win.
With two bookings racing concurrently the exposure is doubled.

**Recommended:** raise `_RESERVE_TIMEOUT_S` from 3.0s to 4.5s. The cost of a
longer timeout is bounded (the burst's later members cover a slow ask, and
`_RESERVE_DEADLINE_MS` is 30s), while the cost of tripping it is a discarded
grant.

## 5. The 09-11 rival, identified

The post-race sheets carry the holder's name, and the row index pins the slot.
Referred to here as **Rival A** — a single named club member, not a foursome,
and not a member of this bot's own group. The name is in the artifacts for
anyone with bucket access; it is left out of this file because `docs/` is
published to a public GitHub Pages site and the person is uninvolved in this
project. Verify with:

```
walden/postrace/20260911_113038_for_20260918/tee_sheet.html   → teeTimeSlots:11
walden/postrace/20260918_113027_for_20260925/tee_sheet.html   → teeTimeSlots:9
```

| race morning | for date | `teeTimeSlots` index | slot | holder |
|---|---|---|---|---|
| 2026-09-11 | 2026-09-18 | 11 | **08:38 AM** | **Rival A** |
| 2026-09-18 | 2026-09-25 | 9 | 08:23 AM | Rival A |

Index 11 = 08:38 AM is not an assumption: the 2026-09-11 post-mortem's own
index→time check records `teeTimeSlots:11` = 08:38 and `:10` = 08:30 on that
same sheet. So **the party who held 08:38 AM on the morning the bot was refused
twelve times for it is the same member now on 08:23 AM** — this week, with the
bot holding 08:38, sitting on 08:23 with one player booked and three seats
still open.

**This corroborates §7e's "same foursome" framing.** The 09-11 sheet's row 11
carries four named players — Rival A plus three others, four
`custom-res-name-link` entries, no TBD placeholders. A real group, filled with
real members, not a member-plus-guests booking like the bot's own (which render
as one name plus three `(Name)` TBD placeholders, `open=0`).

What is new is the shape of their fallback: this week Rival A holds 08:23
**alone**, `holders=[1 name], tbd=[], open=3` as of the post-race sheet (~+28s).
So the group's booker takes the slot first and the other three are added later.
That is a useful behavioural detail — it means a Friday holding that looks like
a lone player early in the race is very likely the front of a foursome, and it
fits a fast human rival (grab, then fill) rather than a late gate.

**What this does and does not establish for Model L vs Model F** (§7e):

- It **weakly favours Model F** (the gate opens at `:01` as on any other day,
  and on prior Fridays 08:38 was simply gone first). The bot sent the *same
  ask at the same offset* (+1014ms) that lost four Fridays running, and won it
  on the one Friday the rival ended up elsewhere. Under Model L — a gate that
  is genuinely late on Fridays — that ask should have been refused today too.
- It **does not settle it.** Reserve 1's 2888ms round trip bounds the club's
  receipt only to `[+1014, +3902]ms`, which straddles a late gate at `:03`. The
  09:23 win is bounded to `[+1017, +2965]ms` and straddles it too. So the one
  morning with the best-placed data point is also the morning whose round trip
  destroyed its discriminating power — the same coupling §7a flagged for
  2026-08-16.
- **What would settle it** is a Friday where a grant lands for one slot while a
  *different* slot is refused in the same club-second (§7d's only
  discriminator), or a named-holder appearance in the observer earlier than
  ~+6s. Neither is available this morning; both are free to wait for.

Nothing here justifies changing the ladder or the aim. Recording the rival's
identity is worth it because it converts "an unidentified fast party" into a
specific, checkable pattern across Fridays.

## 6. Two smaller things

**The racer tasks started ~91s late.** Both logged, at `ERROR`:

```
RACER: task 3/4 ready 90.7s after the 06:28:00 CT login time -
this task started late; move racer_schedule earlier if this recurs
```

It cost nothing today — staging still completed ~36s before the window, and
both bookings won on Reserve #1 — but it consumed the whole 06:28→06:30 budget
the schedule was designed to give. Worth watching; if it recurs, move
`racer_schedule` earlier as the message says. Tasks 0 and 2 of 4 found no
unclaimed booking group and exited 0, as designed.

**The post-race sheet collided, the same way #205's race ledger did.** Both
tasks derived the identical post-race directory
`walden/postrace/20260918_113027_for_20260925/` and wrote to the same
`tee_sheet.html`. The bucket grants only `roles/storage.objectCreator`, so the
second write was refused:

```
11:30:27.692Z  POST .../postrace/..._for_20260925/tee_sheet.html  "200 OK"
11:30:28.071Z  POST .../postrace/..._for_20260925/tee_sheet.html  "403 Forbidden"
11:30:28.071Z  WARNING POSTRACE_SHEET: Could not capture the post-race sheet: ...403...
```

One task's post-race sheet was lost. This is the identical collision class
#205 fixed for `_capture_race_ledger` by scoping the directory with
`CLOUD_RUN_TASK_INDEX`; the post-race path never got the same treatment. Not
harmful today — the surviving copy is what §5 above is built on, and both
bookings were confirmed independently — but on a morning where the two tasks
disagree about who holds what, the lost copy is the evidence.

## 7. Fixes needed

- **Raise `_RESERVE_TIMEOUT_S` 3.0s → 4.5s.** Second morning inside ~115ms of
  the budget, on the winning ask both times. §4.
- **Scope the post-race sheet's GCS directory by `CLOUD_RUN_TASK_INDEX`,** the
  way #205 did for the race ledger. §6.
- **Report a "first named holder" offset per slot in the observations flip
  table,** alongside the existing `(last Empty, first not Empty]` interval.
  That named transition is the only unambiguous signal in the snapshots (§3),
  and it is not currently surfaced.
- **Amend `booking-postmortem` §7f** to say that a `disabled` read carries no
  information about the gate, and that the holder's name lags the grant by
  ~6s. As written, §7f invites exactly the misreading §3 documents.

No change to the booking logic itself. Both races were clean wins on the first
rung, and the first Friday on record to take the requested slot.
