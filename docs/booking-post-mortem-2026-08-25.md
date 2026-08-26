# Booking post-mortem: 2026-08-25

**Target:** Tuesday 2026-09-01, 08:02 AM, 4 players, Northgate
**Window:** Tuesday 2026-08-25 06:30:00 CT (job fires 06:28)
**Outcome: won.** Granted on the first Reserve at +1005ms, chain completed, and
`RESERVATION_CHECK` found `09/01/2026 08:02 AM - 08:10 AM RESERVED` on the
member's own reservations page.
**Code that ran:** `8065197` (#162), merged 2026-08-24. Nothing merged since
until this post-mortem's own fix (below), so `main` at run time is what
executed — no revision archaeology needed.

**Status: this morning is closed.** The exact slot was requested and the exact
slot was booked, on the first of eight available rungs, with no refusals in the
ledger. One log-hygiene fix came out of it (§3); nothing else does.

---

## This morning, in order

| Stage | What happened |
|---|---|
| Login → sheet | Steps 1-4 clean, 06:28:15 → 06:28:50, with one recovered hiccup (§2) |
| Slot scan | 126 rows → 7 candidates (dropped capacity=1, course=64, window=54) |
| Pre-locate | 08:02 AM, `index=4`, `exact=True`, `available=4`, **6 fallbacks** |
| Clock | club **+12ms**, one-way 13ms, 107 probes, 5 transitions, **tick pinned to ±23ms** |
| Lead | 25ms early |
| Fire | Reserve **1/8** at **+1005ms** past the window (aim +1030, less the 25ms lead) |
| Answer | **accepted** — club clock **+1001ms**, round trip **831ms**, 84066 bytes, 0 sheet rows, 0 Reserve buttons, `form=08:02 AM`, `popup=False` |
| Ledger | `club granted 08:02 AM at +1005ms past the window` |
| Outcome | `phase=complete, success=True, attempts=1, totalMs=1991` |
| Verify | `RESERVATION_CHECK: Reservation found` |

The `<eval>` on the accepted response is the accept signature and carries no
`.show()`:

```javascript
executeHoldTimeTimer('300');;stopSheetTimers();;scrollToElement('.tee-time-flag', 50);;
```

Artifacts: `gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/20260825_113003/`
(`ledger.jsonl`, `attempt_01_accepted.xml`). One attempt, so one envelope — the
fallback list was never touched.

### The boundary held again

`serverMsPastWindow: 1001` with an **831ms** round trip brackets the club's
receipt inside `[+1005, +1836]` — the 06:30:01 second, the same second that has
granted every accepted Reserve on record since #150.

| morning | sent | club second | verdict |
|---|---|---|---|
| 08-15 | −60ms | 06:30:00 | refused |
| 08-15 | +1240ms | 06:30:01 | accepted |
| 08-16 | +1023ms | unbracketable (2935ms round trip) | accepted |
| 08-20 | +1006ms | 06:30:01 | accepted |
| 08-21 | +1015ms | 06:30:01 | **refused** (sheet still `disable-div`) |
| **08-25** | **+1005ms** | **06:30:01** | **accepted** |

No new evidence either way on drift: 08-21 remains the one morning the boundary
was caught late. Today's is a same-second repeat of 08-16/08-20.

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
| **08-25** | **race** | **831** |

831ms is comfortably inside the 314–956ms band every other race has landed in;
08-16's 2935ms still stands alone. No change recommended to
`_RESERVE_TIMEOUT_S` (leave at 3.0s).

---

## 2. The course-dropdown fallback recurred a third time

Step 3 raised `element not interactable` on the checkbox dropdown again —
`11:28:39.383Z`, same shape as 08-20 and 08-21: opens the dropdown fine, then
closing it afterward is what fails — `close_button.click()` raises it, because
the CSS selector for the dropdown's close control is broad enough to
occasionally match an element elsewhere on the page that isn't actually
interactable. The code caught it and fell back to verifying the page was
already on Northgate via page text, recovering in ~5.4s against ~70s of
pre-window slack. No cost to the race.

Now recorded on 3 of the 4 mornings that have a written post-mortem (not on
08-13); not on record for mornings without one. Previously called "watch, do
not fix" (08-21 recommendation); worth reconsidering given the repeat, but
that is a UI-timing fix (an explicit wait for the checkbox item to be
clickable, replacing the fixed 0.5s sleep before the click) and is deliberately
**not** part of this post-mortem's fix — flagged for separate scoping.

---

## 3. Fix shipped from this post-mortem

**Silenced per-probe httpx logging during clock-skew measurement.** The 107
HEAD probes in `measure_clock_skew()` each logged their own
`httpx - INFO - HTTP Request: HEAD ... 200 OK` line (`app/providers/walden_http.py`),
adding ~110 lines of pure noise to every race morning's log with zero
diagnostic value beyond the existing "Clock skew measured" summary line. The
`httpx` logger is now dropped to `WARNING` for the duration of the probe loop
only (restored in a `finally`), so the POST-request logging later in the
chain — which the post-mortem skill's §7c timing reconstruction depends on —
is untouched. Landed in #163.

---

## 4. For the skill

Nothing new to add. This morning confirms existing findings (boundary at
06:30:01, round-trip band, course-dropdown fallback is non-fatal) without
adding a new one.
