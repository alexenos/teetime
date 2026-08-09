---
name: booking-postmortem
description: Diagnose why the morning's TeeTime booking run failed. Use when the user says the bot lost the race, didn't get the tee time, "we didn't beat the humans", or asks to check the booking logs for a given morning. Pulls Cloud Run logs and the GCS debug artifacts, classifies the failure, and reports what is and isn't established.
---

# Morning booking post-mortem

The booking job fires at 06:28 CT and the window opens at 06:30:00 CT. Every
morning it loses, the question is the same: did we lose the race, or did we
refuse ourselves? This skill is the repeatable path to that answer.

## 1. Get to latest first

The branch in the working tree is usually behind. Diagnosing against stale code
wastes the run — a fix may already be in.

```bash
git fetch origin && git checkout main && git pull --ff-only && git log --oneline -8
```

## 2. Pull the run

Project `gen-lang-client-0822973627`, Cloud Run service `teetime`, region
`us-central1`. During CDT, 06:30 CT = 11:30 UTC (CST: 12:30 UTC).

**Use the Bash tool, not PowerShell** — PowerShell mangles the quoting inside
the filter and gcloud rejects it with "Unparseable filter".

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="teetime" AND timestamp>="2026-08-09T11:20:00Z" AND timestamp<="2026-08-09T11:40:00Z"' --project=gen-lang-client-0822973627 --format="value(timestamp,textPayload)" --limit=1000 --order=asc | grep -v discord.gateway > run.txt
```

`discord.gateway` DEBUG lines are heartbeat noise and one MESSAGE_CREATE event
can be several hundred lines — always filter them out.

To sweep several mornings at once and see whether a failure is new or chronic,
loop the date and grep for the outcome lines:

```
Firing Reserve|Chain finished|No Reserve accepted|BATCH COMPLETE|Clock skew|arrives as the window|slot finder found
```

## 3. Read the run in order

The run has a fixed shape. Walk it and note where it diverges:

| Stage | Log marker | What to check |
|---|---|---|
| Login / navigate | `BATCH_BOOKING: Step 1..4` | Reached the tee sheet; date selected |
| Slot scan | `Slot scan of N row(s)` | Candidate count, and the `dropped course=/window=` breakdown |
| Pre-locate | `JS slot finder found slot` | `exact=True`, and **how many fallbacks** it kept |
| Clock | `Clock skew measured` | probes, transitions, offset, one-way |
| Lead | `Reserve will be sent Nms early` | Should be tens of ms, not hundreds |
| Fire | `Firing Reserve k/6 ... Nms past the window` | Attempt 1 should be ≈ 0ms or slightly negative |
| Outcome | `Chain finished - phase=..., success=..., blocked=...` | Phase says how far it got |

`phase=complete, success=True` is **not** proof of a booking — the provider
verifies against the member's reservations page afterwards
(`RESERVATION_CHECK:`). Read that line before declaring a win.

## 4. Pull the artifacts

Failures upload to `gs://gen-lang-client-0822973627-teetime-debug-artifacts/`.
The log lines print the exact paths; the naming is
`walden/<reason>/<YYYYMMDD_HHMMSS>/<file>`.

**Use `gcloud storage cp`, not `gsutil`** — gsutil is broken in this
environment (`python3.13: command not found`).

The four that matter:

- `direct_http_blocked_reserve_sent/.../direct_http_response.html` — the club's
  response. **Only the last attempt's**, which is the current diagnostic gap.
- `pre_window_sheet_.../tee_sheet.html` — the sheet as staged before the window.
  The control for any "was this in the response before we sent anything?" test.
- `slot_blocked_by_other_user/.../page.html` — the browser DOM. Beware: the
  direct-HTTP path never touches the browser, so the slot rows here are the
  **pre-window render** and say nothing about who won. The countdown element is
  gone because client-side JS removed it, not because the DOM refreshed.
- `.../screenshot.png` — the rendered page.

## 5. Test claims against the artifacts, don't infer from prose

Two traps have each cost a morning:

**The countdown in a Reserve response is stale.** A Reserve re-renders the view
staged before the window, so it echoes that view's frozen countdown. Confirm by
matching it to the pre-window sheet's `Booking Starts In : 00:0X:XX` — on
2026-08-09 both read 70s. It is logged, never branched on. Don't re-derive
"the window wasn't open" from it.

**Check whether a "refusal" is actually a refusal** before believing it. The
`teeSheetValidationErrorPopup` carries `ui-hidden-container` and no
`aria-hidden`, which makes it *look* like a template that is always present. It
is not: grep the pre-window sheet for the message text. On 2026-08-09 the
pre-window sheet had zero occurrences and the Reserve responses had one, so the
club genuinely refused.

The project's own parser is the right tool for structural questions:

```python
import sys; sys.path.insert(0, r"C:\Users\DaxGarner\Documents\Projects\teetime")
from app.providers.walden_http import parse_html
from app.providers.walden_http_booker import _slot_time_of, _find_blocked_message_in
```

## 6. Classify

- **Lost on the clock** — attempt 1 fired hundreds of ms past the window, or
  `Clock skew unmeasurable`. Look at the probe target and RTTs.
- **Lost on the slot list** — few or zero fallbacks kept, or the scan dropped
  everything. Check the `dropped course=/window=` split.
- **Refused at Reserve** — the club answered "This slot is blocked by another
  user". This is the current standing failure; see below.
- **Refused later in the chain** — `phase=player_count/tbd_guests/book_now`.
  Read `responseMessage`; a club restriction (one round per member per day)
  surfaces here as a `Restriction:` dialog.
- **Chain completed, no reservation** — `success=True` but `RESERVATION_CHECK`
  found nothing. The most dangerous class: it looks like a win in the summary.

## 7. What is already ruled out

Don't re-litigate these; each was established from artifacts, not reasoning:

- **Timing is solved** (as of #141). The clock probe hits a static CSS asset and
  measures cleanly; 2026-08-09 fired at −9ms with a 9ms lead.
- **The chain works.** On 2026-08-04 the same direct-HTTP chain completed and
  booked three times — at T+22s and T+8min, never in the race.
- **The countdown is not the cause** (see above).
- **The blocked popup is not a static-template false positive** (see above).
- **A fresh post-window view does not fix it.** 2026-08-07 refreshed the sheet
  at the window — countdown gone, 86/87 rows reservable — and the club refused
  anyway with the same ViewState and component id. `refresh_at_window` is off
  for this reason.

## 8. Report

State separately: what the run did, what is established from artifacts, what is
hypothesis, and the single cheapest experiment that would discriminate. Note
that per this project there is no local way to exercise booking — testing means
deploying to main behind a flag, so an experiment costs a morning. Say which
morning it would cost and what it would settle.
