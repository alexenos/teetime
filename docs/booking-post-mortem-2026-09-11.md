# Booking post-mortem: 2026-09-11

**Target:** Friday 2026-09-18, 08:38 AM, 4 players, Northgate
**Window:** Friday 2026-09-11 06:30:00 CT (job fires 06:28)
**Outcome: won a slot, lost the requested one.** Twelve Reserves for 08:38 AM,
every one refused, from +1018ms to +3621ms. The first fallback — 08:30 AM — was
granted on its first ask at +5305ms. `RESERVATION_CHECK` confirms
`09/18/2026 08:30 AM - 08:38 AM RESERVED`.
**Code that ran:** `8fa4db0` (#183), current `main` at run time. The first
Friday race on the opening burst (#TBD, merged 2026-09-04).

**Status: partly diagnosed.** The burst was built to answer 09-04's open
question and it did not, because it asked one slot twelve times and then
handed off to a fallback that succeeded immediately. Two new findings, one
withdrawn reading, and one dead end closed.

---

## 1. RACE_LEDGER

`gs://…/walden/race/20260911_113008/`

| # | slot | sent+ms | club sec | RT ms | receipt bounded to | verdict |
|---|---|---|---|---|---|---|
| 1 | 08:38 | 1018 | :01 | 481 | [1018, 1499] | refused |
| 2 | 08:38 | 1118 | :01 | 519 | [1118, 1637] | refused |
| 3 | 08:38 | 1238 | :01 | 564 | [1238, 1802] | refused |
| 4 | 08:38 | 1388 | :01 | 519 | [1388, 1907] | refused |
| 5 | 08:38 | 1569 | :01 | 624 | [1569, 2193] | refused |
| 6 | 08:38 | 1723 | :02 | 565 | [1723, 2288] | refused |
| 7 | 08:38 | 1970 | :02 | 1222 | [1970, 3192] | refused |
| 8 | 08:38 | 2171 | :02 | 961 | [2171, 3132] | refused |
| 9 | 08:38 | 2476 | :02 | 626 | [2476, 3102] | refused |
| 10 | 08:38 | 2870 | :03 | 635 | [2870, 3505] | refused |
| 11 | 08:38 | 3422 | :05 | 1701 | [3422, 5123] | refused |
| 12 | 08:38 | 3621 | :04 | 721 | [3621, 4342] | refused |
| 13 | **08:30** | **5305** | **:06** | 934 | [5305, 6239] | **accepted** |

Opening burst: 12 members, `0+100+220+370+520+700+900+1150+1450+1800+2200+2600ms`
past the aim, **12 target-only, 0 staged fallbacks** — the documented default
since the 09-04 evening ViewState collision. Burst done in 4247ms, nothing
granted. Clock skew −2ms, one-way 13ms, tick pinned to ±24ms, lead 12ms,
`clickDrift=0ms`. Chain `phase=complete, success=True`, `totalMs=6397`.

## 2. The gap: we have never asked in a club-second we proved open

`GATE: after the opening burst - no grant in any club-second asked (:01..:05)
across 1 distinct slot(s)`. Per §7d the only discriminator is a grant for a
different slot in the same club-second. Bounding receipt by
`[sent, sent+roundTripMs]`:

- the last 08:38 ask could have been received **no later than +5123ms** (#11);
- the 08:30 grant was received somewhere in **[+5305, +6239]ms**.

Those ranges do not overlap. The only interval in which we can prove the gate
was open on a Friday is one in which we never asked for the target. That holds
across all four Fridays on record — 08-21 and 08-28 walked off to fallbacks at
`:03` while the first grant to anyone came at `:05`; today the burst pushed
target coverage to `:05` and the first grant came at `:06`.

## 3. Established this morning

**1. The target was genuinely open pre-window, and won during the race.**
The frozen refusal body is a re-render of our own pre-window snapshot (§7d), so
it is a faithful photograph of the sheet at 06:28:47. In it, *every* Northgate
row from 07:45 to 09:15 renders `Empty` with a Reserve button — while the
second course on the same sheet already shows real, named reservations,
byte-identical to the post-race sheet. **The renderer does show holdings.** So
08:38 was not pre-placed or a standing booking; it was taken between 06:28:47
and our first ask. This is a reusable control: *a slot rendering `Empty` in the
pre-window view was genuinely available then.*

**2. Stale staging is not the cause.** Three independent checks:

| check | result |
|---|---|
| ViewState on the 12 refusals | `d249ba63` |
| ViewState on the **accepted** fallback (#13) | **`d249ba63`** — identical |
| index→time map, pre-window vs post-race (+38s) | identical through the block; `teeTimeSlots:11` = 08:38 and `:10` = 08:30 in both |
| every refusal's `<eval>` | `PF('teeSheetValidationErrorPopupVar').show()` — the club's real refusal path, never `view_expired` |

The winning ask rode the same view as the twelve losing ones. The only variable
that changed was which slot was asked for. Non-Fridays accept the same
pre-window-staged body on attempt 1 through the identical code path.

**3. Attempt 1 is the same request every morning; only the day differs.**
Every morning ledger in the bucket, attempt 1:

| date | day | sent | club sec | RT | verdict |
|---|---|---|---|---|---|
| 08-13 | Thu | −7 | :00 | 593 | refused |
| **08-14** | **Fri** | **−14** | **:00** | 647 | refused |
| 08-15 | Sat | −60 | :00 | 741 | refused |
| 08-16 | Sun | 1023 | :03 | 2935 | accepted |
| 08-20 | Thu | 1006 | :01 | 456 | accepted |
| 08-21 | Fri | 1015 | :01 | 465 | refused |
| 08-22 | Sat | 998 | :01 | 529 | accepted |
| 08-25 | Tue | 1005 | :01 | 831 | accepted |
| 08-27 | Thu | 1023 | :01 | 675 | accepted |
| 08-28 | Fri | 1005 | :01 | 444 | refused |
| 08-29 | Sat | 1012 | :01 | 777 | accepted |
| 09-02 | Wed | 1000 | :01 | 775 | accepted |
| 09-03 | Thu | 1013 | :01 | 523 | accepted |
| 09-04 | Fri | 1026 | :01 | 474 | refused |
| 09-05 | Sat | 1010 | :01 | 806 | accepted |
| 09-06 | Sun | 994 | :03 | 2774 | accepted |
| 09-10 | Thu | 1010 | :02 | 1106 | accepted |
| **09-11** | **Fri** | **1018** | **:01** | 481 | **refused** |

Every Friday refused; every other day accepted. **The `:00` second has now been
probed on a Thursday, a Friday and a Saturday and refused all three times** —
08-14 is a real Friday sub-`:01` test, and it lands inside club `:00` with its
round trip to spare.

## 4. Two models still standing

Both fit every artifact, and they call for opposite fixes.

- **Model L — the gate is late.** Friday's sheet opens ~`:05`–`:06`; our early
  refusals are gate refusals; we stop asking just before it becomes winnable.
  Supported by: no Friday grant to anyone before `:05` on any morning.
- **Model F — the rival is fast.** The gate opens at `:01` as every other day
  and 08:38 is gone before +1018ms; the fallbacks sat free the whole time.
  Supported by: 09-04 asked four slots out to **+10.7s** and got nothing while
  09:08 stayed free all morning; today 08:30 was granted on its *first* ask.

Under Model F today cost us something concrete: 08:30 was probably free from
`:01`, and we spent 4.3 seconds and twelve requests on a slot already gone.

## 5. Withdrawn

- *"No Friday has ever been probed before `:01`."* Stated in this session's
  first pass. 08-14 did exactly that, at −14ms, and was refused inside club
  `:00`. Corrected in §3.
- *"Lead the post-burst walk with the target."* Proposed in the same pass on
  Model L reasoning. Under Model F it delays the one slot we could still get.
  Held until the observer reports.

## 6. Round trips (§7b of the skill)

481, 519, 564, 519, 624, 565, 1222, 961, 626, 635, 1701, 721, 934ms. Worst
1701ms, well inside the 3.0s `_RESERVE_TIMEOUT_S`. 08-16's 2935ms still stands
alone. No change.

## 7. Next

The artifacts cannot separate Model L from Model F, because every Friday we
have run is one slot asked repeatedly and a refusal's body describes only our
own snapshot. An independent read of the sheet does separate them. See
`docs/design-observer-and-fanout.md`; the observer job is Phase 0 and wants to
be deployed before 06:28 CT on 2026-09-18.

## 8. Carried forward

- **The "828ms" comment** at `walden_http_booker.py` — still unreconciled.
- **Own-CPU surplus over the idle benchmark** — this morning ran `cpu/wall`
  0.62–1.01 with 90–780ms container CPU per attempt; still unexplained.
- **Target collision policy** for N>1 members — deferred by decision.
