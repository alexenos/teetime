# Booking post-mortem: 2026-09-02

**Target:** Wednesday 2026-09-09, 11:45 AM, 4 players, Northgate
**Window:** Wednesday 2026-09-02 06:30:00 CT (job fires 06:28)
**Outcome: won.** Granted on the first Reserve at +1000ms, chain completed, and
`RESERVATION_CHECK` found `09/09/2026 11:45 AM - 11:53 AM RESERVED` on the
member's own reservations page.
**Code that ran:** `ae8f858` (#166) — `main` at run time. `main` has since
moved to `72e3a3d` (#170, Telegram channel work, merged 06:37 CT, after the
run and unrelated to booking logic) — no revision archaeology needed.

**Status: this morning is closed.** The exact slot was requested and the exact
slot was booked, on the first of eight available rungs, with no refusals in the
ledger. No fix identified or needed.

---

## This morning, in order

| Stage | What happened |
|---|---|
| Login → sheet | Steps 1-4 clean, 06:28:17 → 06:28:51, no hiccups |
| Slot scan | 150 rows → 5 candidates (dropped capacity=4, course=64, window=77) |
| Pre-locate | 11:45 AM, `index=35`, `exact=True`, `available=4`, **4 fallbacks** (11:53 AM, 12:00 PM, 12:08 PM, 12:15 PM) |
| Clock | club **+16ms**, one-way 14ms, 103 probes, 5 transitions, **tick pinned to ±24ms** |
| Lead | 30ms early |
| Fire | Reserve **1/8** at **+1000ms** past the window (aim +1030) |
| Answer | **accepted** — club clock **+1001ms**, round trip **775ms**, 84066 bytes, 0 sheet rows, 0 Reserve buttons, `form=11:45 AM`, `popup=False` |
| Ledger | `club granted 11:45 AM at +1000ms past the window` |
| Outcome | `phase=complete, success=True, attempts=1, totalMs=1915` |
| Verify | `RESERVATION_CHECK: Reservation found` |

The `<eval>` on the accepted response is the accept signature and carries no
`.show()`:

```javascript
executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;
```

Artifacts: `gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260902_113002/`
(`ledger.jsonl`, `attempt_01_accepted.xml`). One attempt, so one envelope — the
fallback list was never touched. A post-race sheet and screenshot were also
saved under `walden/postrace/20260902_113026_for_20260909/`.

### The boundary held again

`serverMsPastWindow: 1001` with a **775ms** round trip brackets the club's
receipt inside `[+1000, +1775]` — the 06:30:01 second, the same second that has
granted every accepted Reserve on record since #150.

| morning | sent | club second | verdict |
|---|---|---|---|
| 08-15 | −60ms | 06:30:00 | refused |
| 08-15 | +1240ms | 06:30:01 | accepted |
| 08-16 | +1023ms | unbracketable (2935ms round trip) | accepted |
| 08-20 | +1006ms | 06:30:01 | accepted |
| 08-21 | +1015ms | 06:30:01 | **refused** (sheet still `disable-div`) |
| 08-25 | +1005ms | 06:30:01 | accepted |
| 08-27 | +1023ms | 06:30:01 | accepted |
| 08-29 | +1012ms | 06:30:01 | accepted |
| **09-02** | **+1000ms** | **06:30:01** | **accepted** |

No new evidence either way on drift: 08-21 remains the one morning the boundary
was caught late. Today's is a same-second repeat of 08-16/08-20/08-25/08-27/08-29.

### Round-trip table (§7b of the skill)

| morning | kind | roundTripMs |
|---|---|---|
| 08-13 | race | 593 |
| 08-14 | race | 647 |
| 08-14 | ad-hoc | 956 |
| 08-15 | race | 741 / 754 |
| 08-15 | ad-hoc | 942 |
| 08-16 | race | **2935** |
| 08-18 | ad-hoc | 522 |
| 08-20 | race | 456 |
| 08-21 | race | 465 / 485 / 314 / 672 / 395 |
| 08-25 | race | 831 |
| 08-27 | race | 675 |
| 08-29 | race | 777 |
| **09-02** | **race** | **775** |

775ms is comfortably inside the 314–956ms band every other race has landed in;
08-16's 2935ms still stands alone. No change recommended to
`_RESERVE_TIMEOUT_S` (leave at 3.0s).

---

## 2. For the skill

Nothing new to add. This morning confirms existing findings (boundary at
06:30:01, round-trip band) without adding a new one. No fix shipped from this
post-mortem — the run was clean end to end.
