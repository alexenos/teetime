# Booking post-mortem: 2026-09-13

**Targets:** Sunday 2026-09-20, Northgate, 4 players each — two bookings, two
requesters:

| booking | requester's member | target | outcome |
|---|---|---|---|
| `45be2c64` | Garner, Ron | 12:08 PM | **booked** 12:08 PM |
| `9dfe167f` | Garner, Melissa | 12:00 PM | **failed** — club restriction |

**Window:** Sunday 2026-09-13 06:30:00 CT (job fires 06:28)
**Code that ran:** `6e105a4`, `main` at run time (latest commit 2026-09-12
17:07 CT). Includes #192 (credential store), #193 (every booking under the
requester's own login), #191/#195 (observer job). #184 still open, so the two
requesters ran sequentially.

**Status: diagnosed.** The race itself was clean — 12:08 PM was granted on the
first Reserve at +1021ms. The second booking failed for a reason that had
nothing to do with the race: the member already held a round on the target
date, booked outside this application, and Northgate allows one per day.

---

## 1. What ran, in what order

Two components each chose a booking, by different rules, and chose differently.
Both behaved as written.

**The racer** — `get_due_bookings` has no `ORDER BY`, and
`execute_bookings_batch` iterates whatever the query returns. Ron's group came
back first:

```
11:28:11.627Z  STARTING BATCH BOOKING  date=2026-09-20  sorted_times=['12:08']  → 45be2c64
11:30:37.379Z  STARTING BATCH BOOKING  date=2026-09-20  sorted_times=['12:00']  → 9dfe167f
```

(`sorted_times` is the sort *within* a group. Each group held one request, so
it is a list of one and says nothing about the order across groups.)

**The observer** — sorts explicitly, `key=(requested_date, requested_time)`,
and takes `scoped[0]`. Earliest tee time wins, so it picked Melissa's 12:00 PM
and logged in under her credential. `observer_phone_number` and
`user_phone_number` are both unset, which is why it logged `2 due bookings for
any requester`.

Group ordering in the racer is an artifact of query planning, not a decision.
Once #184 lands and the sessions run concurrently, it stops mattering.

## 2. RACE_LEDGER

### A — `45be2c64`, 12:08 PM · `gs://…/walden/race/20260913_113006/`

`targetTimestampMs` = 06:29:59.999 CT — the stated window, so these offsets are
in the normal frame.

| # | slot | sent+ms | club sec | RT ms | receipt bounded to | verdict | burst |
|---|---|---|---|---|---|---|---|
| 1 | 12:08 | **1021** | :03 | **2103** | [1021, 3124] | **accepted** | #0 |
| 2 | 12:08 | 1121 | :01 | 506 | [1121, 1627] | refused | #1 |
| 3 | 12:08 | 1241 | :01 | 552 | [1241, 1793] | refused | #2 |
| 4 | 12:08 | 1391 | :01 | 415 | [1391, 1806] | refused | #3 |
| 5 | 12:08 | 1541 | :01 | 434 | [1541, 1975] | refused | #4 |
| 6 | 12:08 | 1721 | :02 | 623 | [1721, 2344] | refused | #5 |
| 7 | 12:08 | 1921 | :02 | 469 | [1921, 2390] | refused | #6 |
| 8 | 12:08 | 2171 | :02 | 295 | [2171, 2466] | refused | #7 |
| 9 | 12:08 | 2471 | :02 | 283 | [2471, 2754] | refused | #8 |
| 10 | 12:08 | 2846 | :03 | 328 | [2846, 3174] | accepted | #9 |

Clock skew −3ms, one-way 12ms, tick pinned to ±23ms, lead 9ms,
`clickDrift=0ms`. Burst of 12, 10 sent, 2 skipped after the grant. Chain
`phase=complete, success=True`, `totalMs=4890`. `RESERVATION_CHECK` confirms
`TEE TIMES (NORTHGATE) 09/20/2026 12:08 PM - 12:15 PM RESERVED`.

`GATE: club :03 - asked 12:08 PM (2 ask(s)) - GRANTED 12:08 PM, 12:08 PM`. Two
grants for one slot — burst #0 and #9, 1825ms apart. The surplus hold was left
to the club's hold timer.

### B — `9dfe167f`, 12:00 PM → 11:53 AM · `gs://…/walden/race/20260913_113119/`

`targetTimestampMs` = **06:31:07.171 CT**, not the window — see §6. In true
terms every ask below went out around 06:31:14, roughly **+74s** past the
window, and the club answered in its `:15` second.

| # | slot | sent+ms¹ | club sec | RT ms | verdict | burst |
|---|---|---|---|---|---|---|
| 1 | 11:53 | 7127 | 31:15 | 1267 | refused | #0 |
| 2 | 11:53 | 6887 | 31:15 | 1617 | refused | #1 |
| 3 | 11:53 | **7039** | 31:15 | 930 | **accepted** | #2 |
| 4 | 11:53 | 7041 | 31:15 | 1122 | refused | #3 |
| 5–12 | 11:53 | 7117–7127 | 31:15 | 885–1371 | refused | #4–#11 |

¹ measured from that fabricated base, not from 06:30:00.

The target 12:00 PM was never asked for: the pre-locate at 06:31:07 dropped it
(`dropped capacity=5`) because it was already taken, and fell back to 11:53 AM
(`exact=False`). The Reserve for 11:53 AM was granted. The chain then failed —
see §3.

## 3. Why booking B failed

Not the race. The Book Now step returned:

> `Reservation Alert: This slot is blocked by another user.; Restriction:`
> `Member: Garner, Melissa is restricted for 1 round(s) on Northgate per Day`

`RESERVATION_CHECK: No reservation listed for this tee time`. Nothing was
booked. This is §6's "refused later in the chain" class, with the restriction
surfacing at `book_now`.

The reason the restriction applied: **Melissa already held 09/20 12:00 PM.**
Her reservations page, fetched at 06:31:28, lists
`09/20/2026 12:00 PM - 12:08 PM Reserved`, and the post-race sheet at 06:30:34
shows 12:00 PM held by `Garner, Melissa` — three seconds before her booking
group started at 06:30:37.

That reservation was not created by this application:

- **It did not exist pre-window.** At 06:28:43 the slot scan recorded no
  capacity drops and listed 12:00 PM as booking A's *first* fallback, so it was
  Available with room for four.
- **Booking A never asked for it.** All ten ledger rows are 12:08 PM.
- **Booking B never got it.** B started after the sheet already showed it held.
- **There was one job invocation**, `11:28:11Z`, and no ad-hoc booking in the
  06:20–06:50 window.

So it was created between 06:28:43 and 06:30:34 by some client other than this
bot. The bot then tried to book a second Sunday round for a member who already
had one, which the club refuses regardless of slot or timing.

Same rule, same wording as #134's 2026-08-04 instance — `Garner, Ron` then,
`Garner, Melissa` now. Enforcement is per member.

## 4. Both sessions used the right login

Worth stating because the failure names one member while the other succeeded,
which reads like a credential mix-up. It was not one.

| booking | member | how established |
|---|---|---|
| `45be2c64` | Garner, Ron | post-race sheet shows 12:08 PM = `Garner, Ron`; `RESERVATION_CHECK` found it on that member's own reservations page |
| `9dfe167f` | Garner, Melissa | the club's restriction names her; the observer independently resolved this same booking's requester to `G00002-S` |

Two distinct members under two distinct credentials. #193 removed the
shared-account fallback — `require_credentials` raises rather than substituting
another login — so a silent mis-login is not reachable on this path.

## 5. The observer captured nothing

First run since deployment, and it exited before taking a snapshot:

```
06:26:30  OBSERVER: sheet is on 'Sunday 13 September', clicking the day tab for 2026-09-20
06:26:30  OBSERVER: date strip offers 7 tab(s)
06:26:30  OBSERVER: no day tab for 2026-09-20 in the date strip - capturing nothing
          rather than photographing the wrong date
```

`walden/observer/` is empty; Cloud Logging shows no observer runs on 09-08
through 09-12. `park_on_date` reaches the target only via a day tab, and the
strip spans the club's 7-day horizon while the target is always the 8th day —
it appears at the window, and the observer parks at 06:24–06:26 by design. So
it can never succeed as written. The racer does not hit this because it selects
the date through the calendar picker. Filed as **#199**; blocks **#190**.

Two useful facts from the run even so:

- The observer held a session on Melissa's credential from 06:26 while booking
  B logged in on the *same* credential at 06:30:39. Neither was evicted —
  concurrent sessions on one credential are fine, which is one of #184's
  stated unknowns.
- Its selenium wire logs wrote the member number and password to Cloud Logging
  in cleartext, because the observer's own `basicConfig` never replicated the
  guard in `app/main.py`. Fixed in #200; the already-written entries still need
  the credential rotated.

## 6. Reading caution on ledger B

`delay_ms = max(0, (execute_at_ct - now_ct))` at `walden_provider.py:1315`.
Booking B reached Step 7 at 06:31:07, past the window, so `delay_ms` clamped to
0, `window_timestamp_ms` became *now*, and every offset in its ledger is
measured from 06:31:07.171 rather than the window. Its `sent+7039ms` was really
about +74s, and its `GATE: club :07` line is not a statement about the gate.

No change proposed. The clamp is only reachable when a session reaches Step 7
after the window, which is a consequence of running the two requesters
sequentially; concurrent sessions all staging at 06:28 and aiming at the same
absolute target never get there. Recorded here so nobody reads that ledger in
the normal frame.

## 7. Round trips (§7b of the skill)

- **A:** 2103, 506, 552, 415, 434, 623, 469, 295, 283, 328ms
- **B:** 1267, 1617, 930, 1122, 1267, 1249, 1207, 1228, 1314, 1295, 885, 1371ms

The winning Reserve took **2103ms** against a 3.0s `_RESERVE_TIMEOUT_S` — the
second-highest race round trip on record after 08-16's 2935ms, and by the
skill's own rule ("leave it at 3.0s until a second morning clears ~2s") this is
that second morning. Still ~900ms of headroom, and unlike 08-16 it is not near
the limit. Flagged for a decision, not changed.

Every other value sits in the familiar 283–1617ms band.

## 8. Withdrawn

- *"The two requesters share one Walden membership, so one booking consumed the
  other's daily round."* Stated in this session's first pass, before the
  post-race sheets were read. They are two distinct members — Ron holds 12:08,
  Melissa holds 12:00 — and Melissa's round came from outside the bot.
  Corrected in §3 and §4.

## 9. Checked against the pre-run hypothesis

`docs/booking-hypothesis-2026-09-13.md`, written the evening before.

| § | predicted | outcome |
|---|---|---|
| 1 | three `SCHEDULED` rows; a self-duplicate at 12:00 PM | **two** due bookings, no duplicate — confirmed by both the batch job and the observer |
| 2a | the duplicate's second request fails with a `Restriction:` | a `Restriction:` did fire, from an external booking rather than a duplicate row |
| 2b | the duplicate's fallback walk takes 12:08 PM | did not happen; B's ladder was 11:38, 11:30, 12:30 |
| 2c | Melissa's group runs first, by insertion order | **reversed** — Ron's ran first |
| 2d | second group fires 06:30:20–06:31:00, without clock-skew compensation | fired 06:31:14, and *did* run full compensation |
| 2e | notifications land together, gated by the slower group | correct — both at 06:31:52–53 |

The premise — a missing duplicate-request guard in `create_booking` — did not
occur this morning. Whether the third Telegram confirmation ever became a
`SCHEDULED` row is unresolved and worth checking before acting on that doc's §4
fix list.

## 10. Carried forward

- **#199** — observer cannot reach its target date. Blocks #190.
- **#200** — observer credential leak; rotate the exposed login separately.
- **#184** — concurrent per-requester sessions. Today's evidence: one credential
  tolerates concurrent sessions; two credentials racing simultaneously is still
  untested. Today's CPU figures (up to 580ms container CPU per response,
  `cpu/wall` 0.71–1.00) favour separate Cloud Run invocations over N browser
  sessions in one process.
- **`get_due_bookings` ordering** — undefined, and not what insertion order
  would predict. Moot once #184 lands.
- **The "828ms" comment** at `walden_http_booker.py` — still unreconciled.
