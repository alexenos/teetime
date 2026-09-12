# Booking hypothesis: 2026-09-13 (targets Sun 2026-09-20)

**Targets, as scheduled tonight (2026-09-12):**

| requester | date | time | players | requested | confirmed |
|---|---|---|---|---|---|
| `8537795292` | 2026-09-20 | 12:00 PM | 4 | 18:01:50 CT | 18:02:01 CT |
| `8501282320` | 2026-09-20 | 12:08 PM | 4 | 18:31:19 CT | 18:31:36.020 CT |
| `8537795292` | 2026-09-20 | 12:00 PM | 4 | 18:30:05 CT | 18:31:36.872 CT |

All three from Telegram chat `-5588592546` (Cloud Run logs, `resource.labels.service_name="teetime"`,
2026-09-12 17:00-19:00 UTC window). The third row is `8537795292` re-asking for the exact slot
they already had — nothing cancelled it first, and nothing in `create_booking` checks for an
existing scheduled booking at the same date/time before creating another. So there are **three**
`SCHEDULED` rows, not two, and one requester (`8537795292`) is unknowingly double-booked against
themselves.

**Window:** Sunday 2026-09-13 06:30:00 CT (job fires 06:28, per the standing 06:28 Cloud Scheduler
trigger).

**Code that will run:** current `main` at hypothesis time (`0aa64b3`) — includes #193
(`fdd1bc3`, every booking runs under the requester's own Walden login) and #192 (`278d458`,
credential store + admin proxy on). #184 (concurrent per-requester execution) is still open.

**Status: pre-run hypothesis**, written 2026-09-12 evening, before the job has fired. This is the
prediction the post-mortem for the 09-13 morning should be checked against — nothing below is
established from artifacts yet.

---

## 1. Why three rows, not a clean two-user race

`create_booking` requires a dedicated Walden credential for the phone number (#193) but never
checks for a pre-existing `SCHEDULED` booking at the same `(phone_number, requested_date,
requested_time)` before writing a new one. `8537795292` asked twice, 29 minutes apart, and
confirmed both — nothing told them they already had a booking for that slot, and nothing merged
or refused the second. The batch job will see three due bookings tomorrow, grouped as:

- **Group X** = `(2026-09-20, 8537795292)`: two requests, both 12:00 PM.
- **Group Y** = `(2026-09-20, 8501282320)`: one request, 12:08 PM.

(`execute_bookings_batch` groups by `(date, phone_number)` —
[booking_service.py:1948](../app/services/booking_service.py#L1948).)

## 2. Predicted mechanics

### 2a. Group X's duplicate is a self-inflicted club-rule refusal, not a race loss

`_book_multiple_tee_times_sync` sorts a group's requests by `target_time` and books them in
sequence, in one session, under one login
([walden_provider.py:1112-1114](../app/providers/walden_provider.py#L1112-L1114)). Both of
`8537795292`'s requests target the identical 12:00 PM slot. Whichever is processed first will
most likely succeed (assuming the slot itself is open); the second collides with Northgate's
one-round-per-member-per-day rule — the club caps this **regardless of when or how the booking is
made** — and comes back a `Restriction:` popup (`_MESSAGE_ID_MARKERS` in
[walden_http_booker.py:96](../app/providers/walden_http_booker.py#L96), narrated in
[walden_provider.py:1816](../app/providers/walden_provider.py#L1816)), independent of whether the
slot was even still available.

**Prediction: one of `8537795292`'s two bookings SUCCEEDS, the other FAILS with a
restriction/already-booked message — for the same member, not a competitor.**

### 2b. The duplicate's fallback walk can eat Group Y's slot before Group Y ever starts

The losing request in 2a still carries `fallback_window_minutes=32`
([schemas.py:58-65](../app/models/schemas.py#L58-L65)), and Northgate slots are 8 minutes apart, so
its fallback ladder is `{11:28, 11:36, 11:44, 11:52, 12:00, 12:08, 12:16, 12:24, 12:32}` —
**which includes 12:08 PM, `8501282320`'s exact target.** If Group X runs first (2d) and its
losing request falls back onto 12:08 before Group Y's session has even logged in, `8501282320`
could lose their slot to their own friend's accidental duplicate request, not to an outside
competitor. Of everything below, this is the most avoidable outcome, and it traces to the missing
duplicate-request guard in 1, not to timing at all.

### 2c. Group ordering has no stated rule, but Group X likely goes first

`get_due_bookings` has no `ORDER BY`
([database_service.py:254-260](../app/services/database_service.py#L254-L260));
`execute_bookings_batch` just iterates whatever the query returns
([booking_service.py:1955](../app/services/booking_service.py#L1955)). That is not a documented
guarantee, but for an append-only table with no updates a Postgres heap scan ordinarily returns
rows close to insertion order — and `8537795292`'s first booking was created at 18:02:01 CT, ~30
minutes before `8501282320`'s only booking (18:31:36.033) or `8537795292`'s second
(18:31:36.872).

**Prediction: Group X (`8537795292`) executes first and gets the pre-window
login/nav/scroll head start described in 2d; Group Y (`8501282320`) goes second, cold.**

### 2d. Whichever group goes second starts from zero, live, during the race

Login, nav to the tee sheet, course select, date select, and pre-scroll all happen *before* the
6:30 wait for whichever group runs first
([walden_provider.py:1135-1241](../app/providers/walden_provider.py#L1135-L1241)) — that lead time
is the entire point of the 06:28 early trigger. The second group's driver is not even created
until the first group's `book_multiple_tee_times` call returns in full. Groups run "one at a time
in this call — concurrent per-requester sessions are a follow-up"
([booking_service.py:1912](../app/services/booking_service.py#L1912), issue #184, still open).

**Prediction: Group Y's first Reserve fires meaningfully later than 06:30:00 — plausibly
06:30:20-06:31:00+ depending on how long Group X's two-request sequence (including its
restriction refusal) takes — with none of the clock-skew compensation or pre-staged click the
06:30 race depends on.**

### 2e. Notifications land together, gated by the slower group

`execute_due_bookings` only sends SMS/Telegram results after the entire `execute_bookings_batch`
call returns ([jobs.py:218-225](../app/api/jobs.py#L218-L225)), i.e. after both groups finish.

**Prediction: all three outcome messages (two for `8537795292`, one for `8501282320`) land in the
chat within moments of each other, timed by whichever group is slowest — most likely Group Y.**

## 3. What to check against tomorrow's run

- [ ] Did Group X actually run first? (Order of `BATCH_BOOKING: === STARTING BATCH BOOKING ===` /
  which phone number's login happens first.)
- [ ] Did `8537795292`'s second request fail with a `Restriction:` popup
  (`phase=player_count/tbd_guests/book_now` per the post-mortem skill's §6), or something else —
  e.g. a plain "blocked by another user" because the slot was simply gone by then?
- [ ] Did the losing request's fallback walk land on 12:08 PM, and if so, before or after
  `8501282320`'s own Reserve was sent for it?
- [ ] What `sentMsPastWindow` did Group Y's first Reserve actually carry, versus Group X's?
- [ ] Did `8501282320` get 12:08 PM, a fallback, or nothing?

If any of this is wrong, revisit 2c's ordering assumption first — read the actual row order out of
the login/`RACE_LEDGER` logs rather than re-deriving it from creation timestamps.

## 4. What this would argue for, if confirmed

- **A duplicate-request guard** in `create_booking` (or `_handle_confirm_intent`): refuse or merge
  a second scheduled booking for the same phone number, date, and time instead of silently
  creating a second row guaranteed to collide at 6:30. Cheapest fix here, independent of #184.
- **Concurrent per-requester execution** (issue #184) — the actual fix for 2d.
- **An explicit `ORDER BY`** (or a stated fairness policy) in `get_due_bookings` — today's group
  order is an accident of query planning, not a decision anyone made.
- Aside: `max_tee_times_per_day: int = 2` exists in
  [config.py:476](../app/config.py#L476) but is never read anywhere else in the app. If it was
  meant to be exactly this guard, it was never wired up.

---

Related: `docs/booking-post-mortem-2026-09-11.md`, `.claude/skills/booking-postmortem/SKILL.md`.
