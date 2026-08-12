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

One caution when the run being diagnosed is not this morning's: a commit to
`main` redeploys, so `main` can have moved past the revision that actually
executed. Check what ran before trusting the source:

```bash
gcloud run revisions list --service=teetime --region=us-central1 --project=gen-lang-client-0822973627 --limit=5 --format="table(name,creationTimestamp)"
```

If a deploy landed between the run and now, read the code at that revision's
commit (`git show <sha>:<path>`, or a worktree) rather than at `main`.

## 2. Pull the run

Project `gen-lang-client-0822973627`, Cloud Run service `teetime`, region
`us-central1`. The job fires at 06:28 CT and the window opens at 06:30 CT —
11:30 UTC during CDT, 12:30 UTC during CST.

**Use the Bash tool, not PowerShell** — PowerShell mangles the quoting inside
the filter and gcloud rejects it with "Unparseable filter".

Set the date once and derive the UTC bounds from it, so the same command works
for any morning and picks the right offset either side of a DST change:

```bash
DAY=2026-08-09
read START END <<<"$(poetry run python - "$DAY" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
day = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=ZoneInfo("America/Chicago"))
fmt = lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
print(fmt(day.replace(hour=6, minute=20)), fmt(day.replace(hour=6, minute=40)))
PY
)"
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"teetime\" AND timestamp>=\"$START\" AND timestamp<=\"$END\" AND textPayload!~\"discord\\.gateway\"" --project=gen-lang-client-0822973627 --format="value(timestamp,textPayload)" --limit=2000 --order=asc > run.txt
```

Python rather than `date -d`: Git Bash here ignores `TZ=America/Chicago` when
parsing (it returns the input unchanged), which would silently query the wrong
five hours. `python -c` also swallows output through this shell — the heredoc
form above is the one that works.

Exclude `discord.gateway` **in the query, not with a local `grep`**. It is
heartbeat DEBUG noise, and `--limit` truncates server-side before a pipe ever
sees the output — filtering locally can silently drop the `Firing Reserve` and
`Chain finished` lines the whole post-mortem depends on.

To sweep several mornings and see whether a failure is new or chronic, loop
`DAY` and grep the result for the outcome lines:

```text
Firing Reserve|Reserve [0-9]+ ->|RACE_LEDGER|Chain finished|No Reserve accepted|BATCH COMPLETE|Clock skew|arrives as the window|slot finder found
```

`RACE_LEDGER: club granted ... at +Nms past the window` is the single most
informative line in a modern run — it states the boundary outright.

## 3. Read the run in order

The run has a fixed shape. Walk it and note where it diverges:

| Stage | Log marker | What to check |
|---|---|---|
| Login / navigate | `BATCH_BOOKING: Step 1..4` | Reached the tee sheet; date selected |
| Slot scan | `Slot scan of N row(s)` | Candidate count, and the `dropped course=/window=` breakdown |
| Pre-locate | `JS slot finder found slot` | `exact=True`, and **how many fallbacks** it kept |
| Clock | `Clock skew measured` | probes, transitions, offset, one-way |
| Lead | `Reserve will be sent Nms early` | Should be tens of ms, not hundreds |
| Fire | `Firing Reserve k/N ... Nms past the window` | Attempt 1 should be ≈ 0ms or slightly negative |
| Each answer | `Reserve k -> <verdict>` | Verdict, club clock, bytes, sheet rows, form slot |
| Boundary | `RACE_LEDGER: club granted ... at +Nms` | Which rung won, and the last that lost |
| Outcome | `Chain finished - phase=..., success=..., blocked=...` | Phase says how far it got |

The sweep means a run now fires several Reserves for the **same** slot before it
touches the fallback list. A refused attempt 1 is expected and is not the story;
which rung was granted is.

`phase=complete, success=True` is **not** proof of a booking — the provider
verifies against the member's reservations page afterwards
(`RESERVATION_CHECK:`). Read that line before declaring a win.

## 4. Pull the artifacts

Failures upload to `gs://gen-lang-client-0822973627-teetime-debug-artifacts/`.
The log lines print the exact paths; the naming is
`walden/<reason>/<YYYYMMDD_HHMMSS>/<file>`.

**Use `gcloud storage cp`, not `gsutil`** — gsutil is broken in this
environment (`python3.13: command not found`).

**Start with the race ledger** — `walden/race/<YYYYMMDD_HHMMSS>/`. It is the
whole run rather than its last frame:

- `ledger.jsonl` — one row per Reserve: `sentMsPastWindow`, `serverMsPastWindow`
  (the club's own clock, from the `Date` header on the POST that decided it),
  `verdict`, `sheetRows`, `reserveButtons`, `reservationFormSlot`, `evalText`,
  `callbackArgs`. Most questions below are a `jq` away and need no HTML at all.
- `attempt_NN_<verdict>.xml` — each attempt's unparsed partial-response,
  `<eval>` scripts and callback parameters included. Those carry whatever the
  club uses to actually *show* a dialog, and no morning before 2026-08-12 has
  them: the parser dropped everything but the re-rendered markup.

The older per-run artifacts:

- `direct_http_blocked_reserve_sent/.../direct_http_response.html` — the club's
  response, and **only the last attempt's**. Superseded by the ledger for any
  run after 2026-08-12; still the only record for the five mornings before it.
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

**The blocked popup is not a verdict.** `teeSheetValidationErrorPopup` and the
text "This slot is blocked by another user" appear in *every* Reserve response —
all five saved from 2026-08-06 to 08-12, including the two where the club had
just granted a tee time. It is emitted as inert markup: no `visible:true`, no
`.show()` anywhere in the payload, `ui-hidden-container` and no `aria-hidden`.
Reading it as a refusal discarded a held slot on 08-08 and again on 08-12.

Its absence from the pre-window sheet proves only that the club renders it in
responses, not in sheets. That is not the same as showing it, and the 08-09
inference built on it does not generalize.

**What separates them is which view came back:**

| | accepted | refused |
|---|---|---|
| size | ~79KB | ~500–680KB |
| `teeTimeSlots:N:` rows | 0 | 79–87 |
| `reserve_button` | 0 | 148–276 |
| `reservationsTable` | present | absent |
| `Reservation at <time>` | populated | absent |

Use the project's classifier rather than re-deriving this. From the repository
root, so the import resolves on any checkout:

```python
from app.providers.walden_http import parse_html
from app.providers.walden_http_booker import classify_reserve_response
verdict, reason = classify_reserve_response(parse_html(markup), markup)
```

It is pinned by `tests/test_reserve_verdict.py` against all five real responses,
kept gzipped in `tests/fixtures/reserve_responses/`. A structural question about
a past morning can be answered there without touching GCS or a booking.

## 6. Classify

- **Lost on the clock** — attempt 1 fired hundreds of ms past the window, or
  `Clock skew unmeasurable`. Look at the probe target and RTTs.
- **Lost on the slot list** — few or zero fallbacks kept, or the scan dropped
  everything. Check the `dropped course=/window=` split.
- **Refused at Reserve inside the first second** — every rung of the sweep so
  far past the window was refused. This is the standing failure, and it is a
  *boundary*, not contention: see below.
- **Refused at Reserve all the way out** — refusals continue past ~2s. That
  would be new, and is the first evidence that would make contention real.
- **Granted, then lost later** — `ledger.jsonl` has an `accepted` row but the
  run still failed. The verdict is no longer the suspect; read the phase.
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
- **The blocked popup is not a verdict** (see above). Superseded 2026-08-12: the
  earlier "the club genuinely refused" reading of it was unsound.
- **A fresh post-window view does not fix it.** 2026-08-07 refreshed the sheet
  at the window — countdown gone, 86/87 rows reservable — and the club refused
  anyway with the same ViewState and component id. `refresh_at_window` is off
  for this reason.
- **The staged request is not malformed.** On 2026-08-08 a byte-identical
  Reserve — same slot, same component id, same ViewState `3d9b5561` — was
  refused at 0ms, refused at 812ms, and **accepted at 1291ms**. 08-12 repeated
  it at 0/817/1239ms. Nothing about the request changed, so "we built it wrong"
  and "the view was stale" are both out.
- **It is not contention.** 08-12 targeted a 5:00 PM nobody wanted; the member's
  own screenshot at 7:46 showed every slot from 4:23 to 6:00 PM still Available.
  The club words *every* early refusal "blocked by another user".

## 7a. The open question

The club refuses for roughly the first second past the window and then accepts.
Two readings remain, and one morning's ledger separates them:

- **A clock difference.** The skew probe measures a *static asset host*, which
  need not share a clock with the application server that decides bookings.
  `serverMsPastWindow` in the ledger reads the `Date` header off the Reserve
  POST itself. If it says ~−1000ms when we sent at our 06:30:00.000, we are
  simply early and the probe target is wrong.
- **A server-side grace period.** If the club's own clock reads 06:30:00 and it
  refuses anyway, the boundary is deliberate and the answer is to aim past it.

Either way the sweep records which offsets were refused and which was granted,
so the boundary narrows to the rung spacing every morning it runs.

## 8. Report

State separately: what the run did, what is established from artifacts, what is
hypothesis, and the single cheapest experiment that would discriminate.

Booking itself still cannot be exercised locally — testing it means deploying to
main behind a flag, so that kind of experiment costs a morning. Say which morning
and what it would settle. But *reading* a response no longer does: anything about
how a response is classified is answerable against
`tests/fixtures/reserve_responses/` in seconds. Check whether the question is
really about the club's behaviour before spending a morning on it.
