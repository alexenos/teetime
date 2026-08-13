# Booking post-mortem: 2026-08-13

**Target:** Thursday 2026-08-20, 08:00 AM, 4 players, Northgate
**Window:** Thursday 2026-08-13 06:30:00 CT (job fires 06:28)
**Reported to the member:** `Unable to book tee time for Thursday, August 20 at
08:00 AM for 4 players: Reservation Alert: This slot is blocked by another user.`
**Code that ran:** `cffa99a` (#146) merged 2026-08-12 20:53 CT — the first
morning with the Reserve ladder, the shape-based verdict, and the race ledger.

---

## Headline

The tee sheet afterwards shows 08:00 AM held by `Garner, Ron` with three
`(Garner, Ron)` TBD guests — the shape of a four-player booking by the
logged-in member — while 07:53 and 08:38 are still Available. So the most
likely reading is that the club gave us the tee time and the run told the
member it had not.

The reason the member was given is provably not a verdict. It is the inert,
view-scoped validation popup, and it is what this code reports for *any*
direct-HTTP failure at or past the Reserve POST, whatever the actual cause.

---

## Established from artifacts, no logs required

**1. The member-facing string is the popup, and the popup rides along on
responses the club accepted.** `find_response_message()` returns exactly the
reported sentence from all five saved Reserve responses — including both the
club *accepted*:

```python
import gzip, pathlib
from app.providers.walden_http import parse_html
from app.providers.walden_http_booker import classify_reserve_response, find_response_message

for p in sorted(pathlib.Path("tests/fixtures/reserve_responses").glob("*.gz")):
    markup = gzip.open(p, "rt", errors="replace").read()
    print(p.name, classify_reserve_response(parse_html(markup), markup)[0],
          repr(find_response_message(markup)))
```

| fixture | verdict | `find_response_message()` |
|---|---|---|
| 20260806_refused | refused | `Reservation Alert: This slot is blocked by another user.` |
| 20260807_refused | refused | `Reservation Alert: This slot is blocked by another user.` |
| **20260808_accepted** | **accepted** | `Reservation Alert: This slot is blocked by another user.` |
| 20260809_refused | refused | `Reservation Alert: This slot is blocked by another user.` |
| **20260812_accepted** | **accepted** | `Reservation Alert: This slot is blocked by another user.` |

`Reservation Alert:` is the popup's `ui-dialog-title` and
`This slot is blocked by another user.` its content label, inside
`teeSheetValidationErrorPopup`. `find_response_message()` skips containers with
`aria-hidden="true"` and containers under a hidden ancestor; this dialog carries
`class="ui-hidden-container"` and **no** `aria-hidden`, so it is always
collected. This is the trap #146 closed for the *verdict*, still open on the
*message*.

**2. Which code path reported the failure.** The string reached the member as
`site_message`, i.e. `chain_result["responseMessage"]` through
`_member_facing_failure` — `walden_provider.py:3285` (chain failed at a phase)
or `:3356` (chain completed, response did not confirm). It cannot have come from
the blocked-verdict branch at `:3221`, which passes `site_message=None` and would
have read `Slot blocked by another user` with no `Reservation Alert:` prefix.

**#146's ladder and classifier are therefore not the source of this failure
report.** Whatever went wrong, the Reserve verdict was not it.

The browser path is also an unlikely source: `_extract_booking_error_message`
filters on `is_displayed()` and joins with `" | "`, where the direct-HTTP path
joins with `"; "` and has no visibility test to apply.

**3. `unchecked` was False.** The message carries no
`(the member's reservations page could not be checked)` suffix. Per branch:

- via `:3356` — `unchecked = held is None`, so `held is False`: the reservations
  page **was read and did not list the tee time**.
- via `:3285` — `held is False`, or `phase ∈ PRE_SUBMIT_PHASES` (nothing reached
  the server, which the tee sheet contradicts), or the browser path ran.

The most consistent reading is that `_reservation_exists` returned False for a
reservation the club's own tee sheet shows. That False is load-bearing twice
over: it suppresses the "could not be checked" caveat, and it sets
`verified_not_reserved`, which is the gate for sending a second Reserve.

**4. This is a standing reporting defect, independent of this morning.**
`find_response_message`'s docstring says "nothing branches on it" — true, but
`_member_facing_failure` *prefers* it over the technical account, so it is the
only thing the member ever sees. Every direct-HTTP failure at or past the
Reserve POST is reported as contention.

---

## Not established — the logs and the ledger could not be pulled

No Cloud Run logs and no GCS artifacts were read for this post-mortem. This
container has no `gcloud` and no credentials: `dl.google.com` returns 403 under
the egress policy, and the GCS JSON API answers anonymous 401 on the debug
bucket. Nothing below is inferred from the run's own record, because none of it
was available.

This morning should be the first with `walden/race/20260813_*/ledger.jsonl`
(`walden_capture_race_ledger` defaults on and the scheduled path passes a target
timestamp), which makes the missing evidence unusually valuable.

---

## Open questions, and what settles each

| Question | Where the answer is |
|---|---|
| Did the club grant the slot, and at which rung? | `RACE_LEDGER: club granted ... at +Nms past the window` |
| Clock difference or server-side grace period (§7a)? | `serverMsPastWindow` in `ledger.jsonl` — first morning it exists |
| How far did the chain get? | `DIRECT_HTTP: Chain finished - phase=..., success=..., blocked=...` |
| Did the reservations check say False, or could it not read? | `RESERVATION_CHECK:` |
| Did the 6:30 run book it, or a later attempt? | count of `/jobs/execute-due-bookings` invocations |

Pull with the skill's recipe at `DAY=2026-08-13`, then:

```bash
gcloud storage cp -r "gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260813_*" .
jq -c '{attempt,sentMsPastWindow,serverMsPastWindow,verdict,sheetRows,reservationFormSlot}' ledger.jsonl
```

**The notification timestamp is itself unresolved.** Discord shows 7:30 AM. If
the member's client is on ET that is 06:30 CT — the race reporting on itself. If
it is on CT, the alert landed roughly an hour after a job that fires at 06:28
with a 300s scheduler deadline and a Reserve budget of 6s, which no code path
explains: it would mean a second attempt or an hour-long session. Worth noting
that a Reserve fired an hour past the window against a slot *we already hold*
would be refused, and the club words that refusal identically.

---

## Hypotheses

**A (favoured).** The ladder crossed the boundary and the club accepted; the
chain or the post-chain verification then misreported. The tee sheet, the ruled
-out blocked-verdict branch, and `unchecked=False` all fit. What remains open is
which step failed and why the reservations page came back empty.

**B.** The 08:00 booking came from something other than the 6:30 race — a later
automated attempt, or a manual booking after the alert. Then the alert may be a
genuine loss and the tee sheet says nothing about the run. The invocation count
and the notification's real local time discriminate.

---

## Recommendations

1. **Stop the view-scoped popup from becoming the member-facing reason.** Apply
   the same test `_find_new_blocked_message` already applies — only a message
   that was not already there counts — to `find_response_message` at
   `walden_http_booker.py:541` and `:658`, or exclude the popup unless something
   in the payload actually shows it. Until then every failure reads as
   contention and the real cause is invisible in the alert.
2. **Log what the reservations check read on a miss.** `_reservation_exists`
   logs the matched row on a hit and nothing on a False.
   `_reservation_row_matches` requires `"tee time"` in the row text *and* the
   date as `MM/DD/YYYY` or `MM/DD/YY`; if the dashboard renders either
   differently the answer is False, which reads as "not booked" on a booking we
   hold and unlocks a second Reserve.
3. Once the ledger is read, the phase names the step to harden.

**Cost:** none of this needs a morning. Item 1 is already settled against
`tests/fixtures/reserve_responses/`. This morning's outcome is in logs and a
ledger that already exist — only a missing ledger would push the question onto
another day.

**Today:** confirm directly on the member's reservations page whether the 08:00
slot is held. It decides whether anyone needs to act, and the row's rendering is
the input to recommendation 2.
