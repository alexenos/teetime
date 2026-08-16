---
name: booking-postmortem
description: Diagnose why the morning's TeeTime booking run failed. Use when the user says the bot lost the race, didn't get the tee time, "we didn't beat the humans", or asks to check the booking logs for a given morning. Pulls Cloud Run logs and the GCS debug artifacts, classifies the failure, and reports what is and isn't established.
---

# Morning booking post-mortem

The booking job fires at 06:28 CT. The window nominally opens at 06:30:00 CT,
but the club's sheet actually opens at 06:30:01 — see §7a, which is why the run
no longer aims at 06:30:00.000. The bot first won the race on 2026-08-15, so
losing is no longer the default — but when a morning does lose, the question is
the same: did we lose the race, or did we refuse ourselves? This skill is the
repeatable path to that answer.

## 0. Set the session up

A fresh remote container (Claude Code on the web) has neither `gcloud` nor a
usable credential nor an installed venv. One script fixes all three, is
idempotent, and takes about 16 seconds cold:

```bash
bash scripts/setup_remote_env.sh
```

Its last line is the answer: `READY` means both access paths work, `PARTIAL`
names the one that does, and `NOT READY` means neither and the warnings above
it say why. Point the environment's setup-script setting at that file and this
happens before you are asked anything.

Check whichever path you are about to use, because they fail independently —
a `gcloud` listing says nothing about whether the venv installed:

```bash
gcloud storage ls gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/
poetry run python scripts/fetch_debug_artifacts.py list --date "$(date -u +%Y%m%d)"
```

Two things worth knowing, because the earlier version of this skill got both
wrong and cost a post-mortem each:

**`gcloud` does install here.** The egress policy refuses `dl.google.com` and
`packages.cloud.google.com`, which is why installing the CLI the documented way
fails — but `dl.google.com` is only a CDN in front of the `cloud-sdk-release`
bucket, and `storage.googleapis.com` is reachable because it is the same host
the debug artifacts come from. `curl` the tarball out of the bucket and you get
a complete SDK, `gsutil` and `bq` included, with its own bundled Python.
Established 2026-08-15; the setup script does exactly this.

**The key file exists and is empty.** The environment sets `GCP_KEY_B64` and
points `GOOGLE_APPLICATION_CREDENTIALS` at `/tmp/gcp-key.json`, but nothing
decodes one into the other, so a test for the file's *existence* passes and
everything downstream fails with an ADC error that names nothing. Test `-s`, not
`-f`. See `docs/debug-artifact-access.md` for the service account and its
grant — read-only, one bucket and the project's logs.

If for some reason the CLI is unavailable, nothing here is blocked:
`scripts/fetch_debug_artifacts.py` reads the same logs and the same bucket over
the JSON APIs with `google.auth` + `httpx`, and every step below gives both
forms.

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

**That command fails for the post-mortem service account** — it needs
`run.revisions.list`, and the grant is deliberately only storage + logging. It
is an IAM gap, not a tooling one; `roles/run.viewer` on the account would close
it. Until then, the logs name the revision themselves:

```bash
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"teetime\" AND timestamp>=\"2026-08-15T11:28:00Z\" AND timestamp<=\"2026-08-15T11:31:00Z\"" --project=gen-lang-client-0822973627 --format="value(resource.labels.revision_name)" --limit=200 | sort -u
```

That gives the revision that served the run (`teetime-00063-779` on 2026-08-15)
but not its commit. Bound that from commit timestamps and confirm against the
run's own behaviour:

```bash
git log --format="%h %ad %s" --date=iso -8
```

Any commit dated after the run was not in it. Confirm rather than assume: the
run logs what the deployed code did, so a field the newest commit introduced —
or a constant it changed — settles which side of the deploy you are on. On
2026-08-15 the ledger's `targetTimestampMs` decoded to 06:29:59.999 CT and no
`RESERVATION_CHECK` line appeared, both of which place the morning run before
#150 (merged 16:32 CT the same day) without ever listing a revision.

## 2. Pull the run

Project `gen-lang-client-0822973627`, Cloud Run service `teetime`, region
`us-central1`. The job fires at 06:28 CT and the window opens at 06:30 CT —
11:30 UTC during CDT, 12:30 UTC during CST.

**The short form, and the only one that works without `gcloud`.** It takes CT
wall-clock times and does the UTC conversion, the DST offset and the
`discord.gateway` exclusion itself:

```bash
poetry run python scripts/fetch_debug_artifacts.py logs \
  --date 2026-08-15 --from 06:20 --to 06:40 --out run.txt
```

Widen `--to` to `08:00` when the question is about what happened *after* the
race — a later manual booking, or the SMS the member got.

**Use the Bash tool, not PowerShell** — PowerShell mangles the quoting inside
the filter and gcloud rejects it with "Unparseable filter".

The `gcloud` form, for a machine that has it. Set the date once and derive the
UTC bounds from it, so the same command works for any morning and picks the
right offset either side of a DST change:

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
| Fire | `Firing Reserve k/N ... Nms past the window` | Attempt 1 should land on the first rung — **≈ +1030ms** since #150, not ≈ 0ms |
| Each answer | `Reserve k -> <verdict>` | Verdict, club clock, bytes, sheet rows, form slot |
| Boundary | `RACE_LEDGER: club granted ... at +Nms` | Which rung won, and the last that lost |
| Outcome | `Chain finished - phase=..., success=..., blocked=...` | Phase says how far it got |

The sweep means a run now fires several Reserves for the **same** slot before it
touches the fallback list. A refused attempt 1 is expected and is not the story;
which rung was granted is.

`phase=complete, success=True` is **not** proof of a booking. Success is a
phrase match on a PrimeFaces partial update — 2026-08-15 returned it on the
words "thank you" — and the one failure class that looks exactly like a win in
the logs is a chain that completed against no reservation. As of #150 the
provider checks the member's reservations page even when the response said yes,
and logs `RESERVATION_CHECK:`. Read that line before declaring a win.

Its **absence** means different things either side of that commit: in a run
after #150 the check should be there, and in a run before it a text-confirmed
booking returned without ever looking. 08-15's morning run has no such line for
that reason, not because anything went wrong. Failing the check is reported,
not enforced — a confirmed booking is not discarded because the page was slow,
so `RESERVATION_CHECK: ... not on the member's reservations page` is an
`ERROR` line in an otherwise successful-looking run.

## 4. Pull the artifacts

Failures upload to `gs://gen-lang-client-0822973627-teetime-debug-artifacts/`.
The log lines print the exact paths; the naming is
`walden/<reason>/<YYYYMMDD_HHMMSS>/<file>`.

List what a morning left, then pull it. This works with or without `gcloud`, and
summarizes any ledger it downloads:

```bash
poetry run python scripts/fetch_debug_artifacts.py list  --date 20260815
poetry run python scripts/fetch_debug_artifacts.py fetch --date 20260815 --out ./artifacts
```

Artifact object names come from `datetime.now()` inside Cloud Run with no `TZ`
set, so **they are stamped UTC**. A 06:30 CT run lands under the same date
either way; an evening run does not.

**Pick the morning's directory, not just the day's.** Ad-hoc bookings write
race ledgers into the same bucket, so a date often has two — `20260815_113004`
is the 06:30 race, `20260815_224814` is a 17:48 CT ad-hoc run. Do not read an
evening ledger as evidence about the boundary: there is no queue outside the
race and the club accepts immediately, so 08-14's evening run was granted at
**−25ms** and 08-15's on the first rung it tried. Neither says anything about
what happens at 06:30. The race is the one whose first Reserve fires near the
window.

To copy by hand, `gcloud storage cp` and `gcloud storage ls` both work. Prefer
them to `gsutil` out of habit — gsutil is broken on the maintainer's local
machine (`python3.13: command not found`), though it does work in a remote
session, where the SDK tarball brings its own Python.

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

**The popup's markup is not a verdict.** `teeSheetValidationErrorPopup` and the
text "This slot is blocked by another user" are carried by Reserve responses
that *accepted* — all five saved from 2026-08-06 to 08-12 and both of 08-15's
morning attempts have it, including the three where the club had just granted a
tee time. In the re-rendered markup it is inert: `ui-hidden-container`, no
`aria-hidden`, no `visible:true`. Reading it as a refusal discarded a held slot
on 08-08 and again on 08-12. `ledger.jsonl` records it as `popupPresent`, which
is `True` on accepted rows too — a presence flag, not an outcome.

It is not in literally every response: 08-15's evening ad-hoc booking was
accepted with `popupPresent=False`. So its *absence* carries no information
either. Neither direction of that flag is a verdict; stop reading it as one.

Its absence from the pre-window sheet proves only that the club renders it in
responses, not in sheets. That is not the same as showing it, and the 08-09
inference built on it does not generalize.

**What does show it is the `<eval>`, and that is a verdict.** Corrected
2026-08-15, the first morning whose raw envelopes were kept: the refused attempt
carried `PF('teeSheetValidationErrorPopupVar').show();;` and the accepted one
carried `executeHoldTimeTimer('300');;stopSheetTimers();;` and no `.show()` at
all. The earlier "no `.show()` anywhere in the payload" was true of what had been
saved, not of what the club sent — the parser dropped `<eval>` before 08-12. So
the popup on a refusal is genuinely displayed to the member, and `evalText` in
the ledger is a second, independent read on the verdict. Do not go back to
matching the popup's *markup*; that is the trap this replaces, not a reprieve
from it.

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

**`markup` there means the extracted markup, not the file you downloaded.** The
`attempt_NN_*.xml` artifacts are the raw `<partial-response>` envelope, and the
classifier reads the re-rendered form inside its CDATA. Hand it the envelope and
a refusal comes back `unknown` — the failure is silent and reads like a finding.
Unwrap it first, which also gives you the `<eval>` scripts:

```python
from pathlib import Path
from app.providers.walden_http import parse_html, parse_partial_response
from app.providers.walden_http_booker import classify_reserve_response
response = parse_partial_response(Path("attempt_01_refused.xml").read_text(errors="replace"))
verdict, reason = classify_reserve_response(parse_html(response.markup), response.markup)
```

Reproduced both of 2026-08-15's verdicts exactly. The older
`direct_http_response.html` artifacts are already-extracted markup and take the
first form.

It is pinned by `tests/test_reserve_verdict.py` against all five real responses,
kept gzipped in `tests/fixtures/reserve_responses/`. A structural question about
a past morning can be answered there without touching GCS or a booking.

## 6. Classify

- **Lost on the clock** — attempt 1 did not land on its rung. Since #150 the aim
  point is +1030ms, i.e. 30ms past the club's 06:30:01 tick, so "early" now means
  *near zero* and not the other way round. Read `Clock skew measured` for the
  tick bracket: a wide one (the probe is budget-bounded at 5s with 20ms spacing
  and should yield several transitions) means the 30ms of margin was aimed with
  a ruler that could not see it. `Clock skew unmeasurable` is the same class.
- **Lost on the slot list** — few or zero fallbacks kept, or the scan dropped
  everything. Check the `dropped course=/window=` split.
- **Refused at Reserve inside the first second** — was the standing failure
  before #150, when attempt 1 went at ~0ms and was spent on a certain no. It is
  a *boundary*, not contention (§7a). A run that still shows this after #150 is
  not aiming where it thinks it is: check the rung offsets, not the club.
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

## 7a. The question that was open, and its answer

**Settled 2026-08-15, the morning the bot first won.** The club refused for
roughly the first second past the window and then accepted, and two readings
were live: a clock difference (the skew probe measures a *static asset host*,
which need not share a clock with the application server) or a deliberate
boundary. 08-15's ledger separates them:

| attempt | sent | club `Date` | verdict |
|---|---|---|---|
| 1 | −60ms | inside the 06:30:00 second | refused |
| 2 | +1240ms | inside the 06:30:01 second | accepted |

The club refused while its **own** clock read 06:30:00. That is the boundary
reading, not the clock reading — the probe was not measuring the wrong host, and
the split holds across every morning on record: refused at −60, −14, −7, 0, 0,
812, 817; granted at 1239, 1240, 1291. The sheet opens at 06:30:01, so every
first Reserve ever sent at ~0ms was spent on a certain no.

#150 acted on this: the first rung is now 1030ms rather than 0, the ladder is
`1030,1250,2000,3000,4500`, the precision wait sleeps to the first rung rather
than to the window, and the clock probe is budget-bounded at 5s with 20ms
spacing so the tick is bracketed tightly enough to aim 30ms past it.

**Read `serverMsPastWindow` as ±1s, not as a millisecond figure.** The HTTP
`Date` header is whole-second resolution and the parser multiplies seconds by
1000 (`walden_http.py`), so a row reading `1` means "somewhere in the 06:30:00
second", not "1ms past". It is precise enough to say which second the club was
in — which is the whole question — and not precise enough for anything finer.

**And read it against `roundTripMs`, because it is stamped when the club
*answers*, not when it receives.** The two are near enough to interchange at a
700ms round trip and not at a slow one. 2026-08-16 fired at +1023ms and the row
says `serverMsPastWindow: 3001` — which is not three seconds of lateness and not
a two-second clock difference, but a `2935ms` round trip putting the answer at
06:30:03 for a request sent at 06:30:01.022. The club's receipt is only bounded
to `[sent, sent+roundTripMs]`; when that span is seconds wide, the row cannot
place the boundary at all. This is why 08-15 could carry the boundary argument
and 08-16 cannot: 08-15's 741ms round trip bracketed the refusal inside the
06:30:00 second, and a 2935ms one brackets nothing. Check the round trip before
reading anything into the second.

**#150 works.** 2026-08-16 was the first race on it: one Reserve, fired at
+1023ms with a tick pinned to ±23ms, accepted first try, and
`RESERVATION_CHECK` found `08/23/2026 08:00 AM RESERVED` on the member's page —
a prime Sunday-morning slot, and the first morning with no refusal in the ledger
at all. Every earlier win came from a later rung after attempt 1 was spent on a
certain no.

What is still open is narrower: whether the 06:30:01 boundary is fixed or drifts
morning to morning. The ladder brackets it either way, and `serverMsPastWindow`
on the granted row records which second won.

## 8. Report

State separately: what the run did, what is established from artifacts, what is
hypothesis, and the single cheapest experiment that would discriminate.

Booking itself still cannot be exercised locally — testing it means deploying to
main behind a flag, so that kind of experiment costs a morning. Say which morning
and what it would settle. But *reading* a response no longer does: anything about
how a response is classified is answerable against
`tests/fixtures/reserve_responses/` in seconds. Check whether the question is
really about the club's behaviour before spending a morning on it.
