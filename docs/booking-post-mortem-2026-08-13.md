# Booking post-mortem: 2026-08-13

**Target:** Thursday 2026-08-20, 08:00 AM, 4 players, Northgate
**Window:** Thursday 2026-08-13 06:30:00 CT (job fires 06:28)
**Reported to the member:** `Unable to book tee time for Thursday, August 20 at
08:00 AM for 4 players: Reservation Alert: This slot is blocked by another user.`
**Code that ran:** `cffa99a` (#146) merged 2026-08-12 20:53 CT — the first
morning with the Reserve ladder, the shape-based verdict, and the race ledger.

**Status: open.** No Cloud Run logs and no debug artifacts have been read. The
findings below stand on the checked-in fixtures and on the code alone; the
questions in "Still open" are the ones the artifacts settle, and none of them
should be acted on before that.

---

## The fact that reframes this morning

The bot books as Ron Garner. So does the member's father, by hand, from the same
account, racing the same 06:30 window for the same slots — on this morning
included.

That has three consequences, and they matter more than anything the screenshot
shows:

1. **The tee sheet cannot say who booked it.** `Garner, Ron` plus three
   `(Garner, Ron)` TBD guests at 08:00 is what a four-player booking on this
   account looks like whether the bot made it or his father did. Any reading of
   the screenshot as evidence that the bot's Reserve succeeded is withdrawn.
2. **"Blocked by another user" may be literally true this morning.** If his
   father completed the booking first, the club's refusal is honest and the bot
   simply lost the race — the first morning of the five where contention is a
   live explanation rather than a phrase the club uses for everything.
3. **The verification oracle is no longer sound** — see below. This one holds
   regardless of what happened this morning.

---

## Established from the checked-in fixtures

**1. The member-facing sentence is the inert popup, not a verdict.**
`find_response_message()` returns exactly the reported sentence from all five
saved Reserve responses, including both the club *accepted*:

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
`aria-hidden="true"` or a hidden ancestor; this dialog carries
`class="ui-hidden-container"` and **no** `aria-hidden`, so it is always
collected. This is the trap #146 closed for the *verdict*, still open on the
*message*.

The point survives the reframing above: even if the club really was refusing
this morning, the alert the member received is not evidence of it. The same
sentence arrives when the club accepts.

**2. Which code path reported the failure.** The string reached the member as
`site_message`, i.e. `chain_result["responseMessage"]` through
`_member_facing_failure` — `walden_provider.py:3285` (chain failed at a phase)
or `:3356` (chain completed, response did not confirm). It cannot have come from
the blocked-verdict branch at `:3221`, which passes `site_message=None` and
would have read `Slot blocked by another user` with no `Reservation Alert:`
prefix.

So whatever happened, **the run did not report a refused Reserve verdict.** It
reported a chain-step failure or an unconfirmed booking. #146's ladder and
classifier are not the source of this alert. The browser path is an unlikely
source too: `_extract_booking_error_message` filters on `is_displayed()` and
joins with `" | "`, where the direct-HTTP path joins with `"; "` and has no
visibility test available to it.

**3. `unchecked` was False.** The alert carries no
`(the member's reservations page could not be checked)` suffix. Via `:3356` that
means `held is False`; via `:3285` it means `held is False`, or
`phase ∈ PRE_SUBMIT_PHASES`, or the browser path ran. So on the likeliest
branches the reservations page was read and did **not** list the tee time. That
False is load-bearing twice: it suppresses the "could not be checked" caveat,
and it sets `verified_not_reserved`, which is the gate for sending a second
Reserve.

**4. A standing reporting defect, independent of this morning.**
`find_response_message`'s docstring notes that nothing branches on it — true,
but `_member_facing_failure` *prefers* it over the technical account, so it is
the only thing the member sees. Every direct-HTTP failure at or past the Reserve
POST is reported as contention, whatever the cause.

---

## The shared login breaks the verification oracle

`_reservation_exists` asks the member's reservations page whether the tee time
was booked. With two actors on one account that question no longer means what
the code needs it to mean:

- **False success.** If his father's booking has landed by the time the check
  runs, the page lists the tee time and the provider returns
  `success=True` — "blocked at phase X but the reservation is on the member's
  reservations page" (`walden_provider.py:3209`). The bot would claim a slot it
  did not win. The member gets a confirmation either way, so this is benign for
  the golf and corrosive for the diagnosis: it would look like the bot winning
  races it is losing.
- **Misleading False.** `held is False` only means nothing was booked *yet* — it
  depends on whether his father had finished clicking through player count, three
  TBD guests, and Book Now when the check ran, seconds after the window. It is
  not evidence that the bot's own Reserve failed.

Both directions are silent. Nothing in the reservations row identifies which
session created it.

---

## Still open — what the artifacts settle

Nothing below has been checked. `walden/race/20260813_*/` should be the first
race ledger ever written (`walden_capture_race_ledger` defaults on, and the
scheduled path passes a target timestamp), which makes this morning's artifacts
unusually valuable even if the race was lost fairly.

| Question | Where the answer is |
|---|---|
| Did the club grant a slot at any rung, or refuse the whole ladder? | `RACE_LEDGER: club granted ... at +Nms` vs. `every attempt refused, out to +Nms` |
| Do refusals continue past ~2s? (the first real evidence for contention) | `ledger.jsonl` — `sentMsPastWindow` on the last refusal |
| Clock difference or server-side grace period (§7a of the skill) | `serverMsPastWindow` — the club's own `Date` header, first morning it exists |
| How far did the chain get, and which step broke | `DIRECT_HTTP: Chain finished - phase=..., success=..., blocked=...` |
| Did the reservations check say False, or fail to read | `RESERVATION_CHECK:` |
| Was 08:00 already gone when the bot scanned the sheet | `pre_window_sheet_.../tee_sheet.html`, `Slot scan of N row(s)` |
| Did the 6:30 run report this, or a later attempt | count of `/jobs/execute-due-bookings` invocations |
| Is the 7:30 alert an hour late, or 06:30 CT on an ET clock | the log timestamp of the `send_booking_failure` call |

A refusal at attempt 1 (+0ms) cannot be explained by a *completed* manual
booking — nobody clicks through four players and Book Now in zero milliseconds.
It could be explained by a *hold* his father's session had already taken. Which
of those the morning was is exactly what the ledger's per-attempt timeline, read
against his father's plausible click times, decides.

### Fetching them

This post-mortem was written in an environment with no `gcloud` binary and no
credentials (`dl.google.com` refused by egress policy, anonymous 401 on the
debug bucket), so the fetch has to run somewhere authenticated:

```bash
DAY=2026-08-13   # then the skill's log recipe for START/END
gcloud storage ls -r "gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/**/20260813_*"
gcloud storage cp -r "gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260813_*" .
jq -c '{attempt,sentMsPastWindow,serverMsPastWindow,verdict,sheetRows,reservationFormSlot}' ledger.jsonl
```

---

## Hypotheses, re-ranked

**B (now favoured). His father won the slot by hand.** The club's refusal was
truthful, the ladder was refused all the way out, and the bot lost a fair race.
Signature: no `accepted` row in the ledger, refusals continuing well past the
~1s boundary the previous five mornings established, and 08:00 unavailable on
any post-window sheet the run captured.

**C (new, and cheap to test off-race). Self-contention on one member account.**
Two concurrent logins as the same member — his father's browser and the bot's
Selenium session — may share or clobber server-side JSF state on the club's
portal, and the club's "another user" could be the bot's own account in another
session. This would predict refusals that track *his father being awake*, which
is testable without a booking: log in twice as the same member, stage a view in
session A, log in as session B, then act in session A. `scripts/probe_direct_http.py`
already does read-only component work with an adopted cookie jar and is the
natural place to extend. Needs `WALDEN_MEMBER_NUMBER` / `WALDEN_PASSWORD`.
Worth asking whether he was logged in on 08-06/07/09 — and specifically whether
he was *not* on 08-12, the 5:00 PM afternoon slot that still saw ~1.2s of
refusals with nobody competing.

**A (weakened). The bot got the hold and misreported it.** Still consistent with
findings 2 and 3, but it no longer has the screenshot behind it. The ledger's
verdict rows decide it outright.

---

## Recommendations — none to be acted on before the artifacts are read

1. **Stop the view-scoped popup from becoming the member-facing reason.** Apply
   the test `_find_new_blocked_message` already applies — only a message that was
   not already there counts — to `find_response_message` at
   `walden_http_booker.py:541` and `:658`, or exclude the popup unless something
   in the payload actually shows it. This one is settled on fixtures and does not
   depend on the artifacts; it is safe whichever way this morning went, and until
   it lands every failure alert reads as contention.
2. **Make the reservations check say what it saw**, and treat its answer as
   account-wide rather than bot-specific. It logs the matched row on a hit and
   nothing on a False, and `_reservation_row_matches` requires `"tee time"` plus
   an `MM/DD/YYYY`-style date. With a second human on the login, a `True` is not
   proof the bot booked it and a `False` is not proof nobody did — anything
   gated on it (`verified_not_reserved`, and the success report at `:3209`)
   inherits that.
3. **Decide what losing to a family member should do.** Both actors want the same
   tee time and only one can have it. If his father's manual booking is an
   acceptable outcome, the honest report is "already booked on your account at
   08:00", not "unable to book" — and the reservations check already has the
   information to say so.
