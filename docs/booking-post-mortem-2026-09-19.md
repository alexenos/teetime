# Booking post-mortem: 2026-09-19

**Targets:** Saturday 2026-09-26, Northgate — 12:08 PM and 12:15 PM, 4 players
each, two separate racer tasks in the same batch.
**Window:** Saturday 2026-09-19 06:30:00 CT (job fires 06:28; batch started
06:28:00, per-request pre-locate/staging at 06:28:18).
**Outcome: won both.** 12:08 PM granted on attempt 1 at +1004ms; 12:15 PM
granted on attempt 3 at +1234ms. Both confirmed by `RESERVATION_CHECK` against
the member's own reservations page, not just the "thank you" phrase match.
**Code that ran:** `4550f6a` (#213), merged 2026-09-18 13:02 CT. Nothing has
merged since, so `main` is what executed — no revision archaeology needed.
This is the first morning racing for two slots at once since #184/#204 split
the race into the `teetime-racer` job (one task per requester).

**Status: this morning is closed.** Clean win on both, corroborated by the
observer, with one number worth tracking rather than fixing.

---

## This morning, in order

Clock skew was measured independently per task: club +15ms (one-way 11ms,
tick ±22ms) for the 12:08 PM task, club +2ms (one-way 14ms, tick ±24ms) for
12:15 PM.

| Stage | 12:08 PM | 12:15 PM |
|---|---|---|
| Login → sheet | Steps 1-4 clean, shared run, 06:28:03.9 → 06:28:14.9 | same run |
| Slot scan | 154 rows → 5 candidates (dropped capacity=4, course=67, window=78) | 154 rows → 6 candidates (dropped capacity=3, course=67, window=78) |
| Pre-locate | `index=39`, `exact=True`, `available=4`, 4 fallbacks | `index=40`, `exact=True`, `available=4`, 5 fallbacks |
| Lead | 26ms early | 16ms early |
| Opening burst | 12 Reserves fired concurrently | 12 Reserves fired concurrently |
| Fire (winning rung) | attempt **1**, +1004ms past the window | attempt **3**, +1234ms past the window |
| Answer | **accepted** — club clock +4001ms, round trip **3636ms**, 86137 bytes, `form=12:08 PM`, `popup=False` | **accepted** — club clock +4001ms, round trip **3394ms**, 91963 bytes, `form=12:15 PM`, `popup=False` |
| Last refusal | +3604ms | +3614ms |
| `GATE:` | club `:03` asked (9), granted none; club `:04` asked (3), **GRANTED** | club `:03` asked (10), granted none; club `:04` asked (2), **GRANTED** |
| Ledger | `club granted 12:08 PM at +1004ms past the window; last refusal was +3604ms` | `club granted 12:15 PM at +1234ms past the window; last refusal was +3614ms` |
| Chain | `phase=complete, success=True, blocked=False, attempts=12, totalMs=5914` | `phase=complete, success=True, blocked=False, attempts=12, totalMs=5088` |
| Verify | `RESERVATION_CHECK: Reservation found ... 12:08 PM - 12:15 PM ... RESERVED` | `RESERVATION_CHECK: Reservation found ... 12:15 PM - 12:23 PM ... RESERVED` |

Both accepted responses carried the accept `<eval>` signature, no `.show()`:

```javascript
executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;
```

Every refusal carried `PF('teeSheetValidationErrorPopupVar').show();;` — the
two are separated cleanly, per §5 of the skill, by the `<eval>`, not by the
popup markup (which appears on refusals and, per the granting response's own
log line, was echoed once on 12:15's grant too: *"The granting response also
carried 'Slot blocked by another user'; treating it as stale for the rest of
the chain"* — expected, not a fault).

Artifacts:
`gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260919_113007_teetime-racer-pzq5h_2/`
(12:15 PM, 12 ledger rows, 7 raw payloads stored) and
`.../20260919_113007_teetime-racer-pzq5h_3/` (12:08 PM, 12 ledger rows, 5 raw
payloads stored).

### The gate opened at club `:04` for both slots

`GATE:` lines are explicit this morning, no boundary inference needed:

| slot | club `:03` | club `:04` |
|---|---|---|
| 12:08 PM | 9 asked, 0 granted | 3 asked, **granted** |
| 12:15 PM | 10 asked, 0 granted | 2 asked, **granted** |

Consistent with every prior morning's reading of the boundary — refusals
inside `:03` are ambiguous (gate or taken), refusals from `:04` on (if there
were any after the grant) would be taken slots. Here the grant itself landed
in the first ask of `:04` for both requests, so there is nothing past the
grant to classify.

### The observer corroborates: neither slot was pre-held

Per §7f this is a standing check, run whether or not the boundary is in
question. The observer (`20260919_112709_teetime-observer-nz4ht`,
`northgateRowCount: 952`, a real read) shows both target rows:

| course | idx | slot | state at −500/+3745ms | flip |
|---|---|---|---|---|
| Northgate | 39 | 12:08 PM | empty → disabled | `(-500, +3745]ms` |
| Northgate | 40 | 12:15 PM | empty → disabled | `(-500, +3745]ms` |

Both read `empty` (available, Reserve button present) and flipped to
`disabled` by the observer's first post-window snapshot — the same window in
which our own Reserves were accepted. That is the win signature, not the
"never Empty" (pre-held) signature seen on contested Friday slots in earlier
mornings. One caveat: the `-500ms` snapshot itself has `refreshOk: false`
("re-render did not land within 4.0s; these bytes may repeat the previous
snapshot"), so it may just be repeating the pre-window read rather than a
live one at `-500ms` — this doesn't change the conclusion, since the
pre-window sheet is itself a valid control for "was this open before the
race" per §7e, and the first *reliable* post-window read already shows the
slots taken by +3745ms, consistent with our own grants at +1004/+1234ms.

### Round trip: fast for a single slot, slower under two concurrent races

Both accepted responses show round trips (3636ms, 3394ms) that look alarming
against the skill's watch line for `_RESERVE_TIMEOUT_S = 3.0s` (§7b). That
constant does not apply here: burst-mode opening fires (all 12 attempts per
slot, sent concurrently) use `_RESERVE_OPENING_TIMEOUT_S = 10.0`
(`walden_http_booker.py:260`), not the serial ladder's 3.0s. No request was
in danger of timing out; this is not a new instance of the §7b risk.

It is still worth a note for the log: this is the first morning with two
slots racing concurrently in the same batch, and the winning round trips
(3.3-3.6s) are the slowest *accepted* round trips on record — previous
mornings topped out under 3.0s even on contested Fridays. Both wins landed
comfortably inside the 10s opening budget, so nothing to fix today. If
concurrent-slot mornings become routine, this is the number to keep watching
rather than the serial-ladder table in §7b.

---

## Established vs hypothesis

**Established**

- Both bookings were due and both were granted: 12:08 PM at +1004ms, 12:15 PM
  at +1234ms, both against club `:04`.
- Both are real reservations, not a phrase-match false positive —
  `RESERVATION_CHECK` found each one by name on the member's own reservations
  page.
- Neither slot was pre-held: the observer shows both `empty` pre-window and
  `disabled` only after the grant.
- The 3.6s/3.4s round trips were within budget (`_RESERVE_OPENING_TIMEOUT_S =
  10.0s` for burst fires), not a near-miss on any active timeout.

**Hypothesis**

- That round trips trend slower specifically because two slots race
  concurrently in one batch (shared container CPU, network). Plausible, not
  established — this is the only two-slot morning on record, so there is no
  second data point yet.

**Cheapest experiment that would discriminate:** none needed to costs a
morning. The next two-slot (or more) morning will either repeat the
3-3.5s-ish round trips or not; reading `roundTripMs` on it settles whether
this is a pattern or one data point, exactly as §7b already does for the
serial-ladder table.

## Recommendations

1. **No change to the race path.** Both requests won cleanly, on the first or
   third rung, well inside every active timeout budget.
2. **Keep reading `roundTripMs` on every race, including burst-mode
   concurrent-slot mornings**, and start a table for those the way §7b tracks
   the serial ladder — one data point today, worth two or three before
   drawing a conclusion.
3. **No IAM or infrastructure follow-up.** This morning needed nothing beyond
   the storage + logging grant the post-mortem account already has.
