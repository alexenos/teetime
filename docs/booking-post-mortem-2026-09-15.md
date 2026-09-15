# Booking post-mortem: 2026-09-15

**Targets:** Tuesday 2026-09-22, Northgate, 4 players each — two bookings, two
requesters, both racing the same window as separate `teetime-racer` job tasks:

| booking | requester's member | target | outcome |
|---|---|---|---|
| task 1 (`9eae923b`) | — | 08:10 AM | **booked** 08:10 AM |
| task 0 (`6f557be1`) | — | 08:02 AM | **booked** 08:02 AM |

**Window:** Tuesday 2026-09-15 06:30:00 CT (job fires 06:28)
**Code that ran:** `b67269c` — "Fan out the 6:30 race: one Cloud Run job task
per requester (#184) (#204)" — `main` at run time, and the first race run
under this architecture. The batch booking moved from an HTTP endpoint on the
`teetime` Cloud Run *service* to the `teetime-racer` Cloud Run *job*, one task
per requester, racing concurrently rather than sequentially (contrast
2026-09-13, the last morning with two requesters, where #184 was still open
and the two ran one after another).

**Status: diagnosed.** Both bookings won, both confirmed by
`RESERVATION_CHECK`. Concurrency of the fanned-out tasks surfaced a real bug
in the debug-artifact path, not in the booking itself: the two tasks derived
the same GCS race directory from a whole-second timestamp, collided on
filenames, and one task's `ledger.jsonl` was silently dropped (403s).
Separately, the tooling used to *read* this morning's logs was itself stale
for the new job architecture and initially misreported the morning as having
no booking at all.

---

## 1. The initial misdiagnosis, and why it happened

The automated post-mortem's first pass reported "no booking was scheduled for
2026-09-15." That was wrong, and worth recording because it is exactly the
trap `booking-postmortem` §0 already warns about for a different tool, one
level up.

`scripts/fetch_debug_artifacts.py logs` queried
`resource.type="cloud_run_revision" AND resource.labels.service_name="teetime"`
— correct through 2026-09-14, when the whole batch ran inside that service.
As of this morning's #184/#204 deploy, the race runs as the `teetime-racer`
Cloud Run **job**, a different resource type entirely
(`resource.type="cloud_run_job"`, keyed by `job_name` not `service_name`).
The query returned a clean, valid, **zero-row** response — no error, nothing
to distinguish it from an actual quiet morning — which read exactly like "no
booking ran." A full day's query (00:00–23:59) against the same stale filter
also came back empty, reinforcing the wrong conclusion instead of raising a
flag.

It was caught only because the user pushed back after the first (wrong)
report, prompting a cross-check of the GCS artifact listing directly
(`fetch_debug_artifacts.py list --date 20260915`), which showed a full race:
`walden/race/20260915_113005/`, two `walden/postrace/` directories, and
`walden/observer/2026-09-22/`. Querying `resource.type="cloud_run_job"`
against the same window then surfaced the whole run, including two
`RACER: task N/4` lines and two independent `RACE_LEDGER` sequences.

**Fixed in #206**, which makes `fetch_debug_artifacts.py logs` query the
`teetime` service and the `teetime-racer`/`teetime-observer` jobs together by
default, and updates `booking-postmortem`'s §1–§2 with the same caution:
don't trust an empty `logs` result without checking the GCS listing first.

## 2. Both races, ledger by ledger

Both tasks logged in, navigated, and staged independently and concurrently —
same job execution, different `CLOUD_RUN_TASK_INDEX`. Round trips throughout
are in the low hundreds of ms to ~1.2s, comfortably inside the 3.0s
`_RESERVE_TIMEOUT_S` (§7b of the skill) — no margin concern from either race.

### Task 1 — 08:10 AM, member `9eae923b`

Slot scan: 6 candidates, exact match at index 5, 5 fallbacks behind it. Clock
skew: club +18ms from us, one-way 12ms, 114 probes, tick pinned to ±22ms;
Reserve fires 30ms early.

| attempt | sent+ms | verdict | roundTripMs | club clock |
|---|---|---|---|---|
| 1 | +1000 | **accepted** | 918 | +1001ms |
| 2 | +1100 | refused | 722 | +1001ms |
| 3 | +1220 | refused | 823 | +2001ms |
| 4 | +1370 | refused | 792 | +2001ms |
| 5 | +1550 | accepted (surplus hold) | 564 | +2001ms |
| 6 | +1700 | accepted (surplus hold) | 219 | +1001ms |
| 7 | +1900 | accepted (surplus hold) | 113 | +1001ms |

`RACE_LEDGER: club granted 08:10 AM at +1000ms past the window; last refusal
was +1370ms`. Chain finished `phase=complete, success=True, totalMs=4565`.
`RESERVATION_CHECK` confirmed `TEE TIMES (NORTHGATE) 09/22/2026 08:10 AM -
08:18 AM RESERVED`.

### Task 0 — 08:02 AM, member `6f557be1`

Slot scan: 7 candidates, exact match at index 4, 6 fallbacks behind it. Clock
skew: club +7ms from us, one-way 12ms, 113 probes, tick pinned to ±22ms;
Reserve fires 19ms early.

| attempt | sent+ms | verdict | roundTripMs | club clock |
|---|---|---|---|---|
| 1 | +1011 | **accepted** | 1230 | +2001ms |
| 2 | +1111 | refused | 542 | +1001ms |
| 3 | +1231 | refused | 729 | +1001ms |
| 4 | +1381 | refused | 737 | +2001ms |
| 5 | +1531 | refused | 639 | +2001ms |
| 6 | +1711 | refused | 703 | +2001ms |
| 7 | +1911 | refused | 533 | +2001ms |
| 8 | +2161 | accepted (surplus hold) | 180 | +2001ms |

`RACE_LEDGER: club granted 08:02 AM at +1011ms past the window; last refusal
was +1911ms`. Chain finished `phase=complete, success=True, totalMs=4337`.
`RESERVATION_CHECK` confirmed `TEE TIMES (NORTHGATE) 09/22/2026 08:02 AM -
08:10 AM RESERVED`.

## 3. Timing table

Offsets from window open, 06:30:00.000 CT. Both tasks ran the same steps
concurrently in one job execution; times below are wall-clock UTC.

| Step | Task 1 (08:10 AM) | Task 0 (08:02 AM) |
|---|---|---|
| Task claimed | 11:26:43.892 | 11:26:43.980 |
| Login (Step 1) | 11:28:08.975 | 11:28:07.072 |
| Course/date select (Steps 2–4) | done by 11:28:20.419 | done by 11:28:18.738 |
| Slot scan / pre-locate | 11:28:22.385 — idx 5, exact, 5 fallbacks | 11:28:24.058 — idx 4, exact, 6 fallbacks |
| Clock skew measured | +18ms, one-way 12ms, ±22ms | +7ms, one-way 12ms, ±22ms |
| Lead | fires 30ms early | fires 19ms early |
| Reserve 1 fired | +950ms past window (11:30:00.949) | +961ms past window (11:30:00.960) |
| Reserve 1 verdict | **accepted** @ +1000ms, RT 918ms | accepted @ +1011ms, RT 1230ms |
| RACE_LEDGER boundary | granted +1000ms; last refusal +1370ms | granted +1011ms; last refusal +1911ms |
| Chain finished | 11:30:06.767, totalMs=4565 | 11:30:06.804, totalMs=4337 |
| RESERVATION_CHECK confirmed | 11:30:11.206 (~4.4s after lookup started) | 11:30:11.522 (~4.7s after lookup started) |
| Postrace sheet saved | 11:30:21.557 | 11:30:22.626 |
| BATCH_JOB complete | 11:30:22.304 | 11:30:23.363 |

## 4. The GCS artifact collision

Both tasks wrote to the same race directory, `walden/race/20260915_113005/`,
because `_capture_race_ledger` in `app/providers/walden_provider.py` derived
`run_id` from `datetime.now().strftime("%Y%m%d_%H%M%S")` alone — whole-second
resolution, no per-task discriminator. Both tasks' attempts 1–4 happened to
share identical verdicts at the same attempt number (both refused at 2–4,
both accepted at 1), which meant identical filenames
(`attempt_01_accepted.xml`, `attempt_02_refused.xml`, …, and `ledger.jsonl`
itself, which carries no attempt number at all).

The bucket grants the writer only `roles/storage.objectCreator` — create, not
overwrite. Task 1 wrote first and every one of its writes to those names
succeeded (`200 OK`). Task 0's writes to the *same* names then came back
`403 Forbidden`:

```
11:30:05.918Z  POST .../ledger.jsonl                "200 OK"   (task 1)
11:30:05.937Z  POST .../ledger.jsonl                "403 Forbidden"  (task 0)
11:30:06.032Z  POST .../attempt_01_accepted.xml      "200 OK"   (task 1)
11:30:06.035Z  POST .../attempt_01_accepted.xml      "403 Forbidden"  (task 0)
... same pattern for attempts 2, 3, 4 ...
```

**Net effect: task 0's `ledger.jsonl` never reached GCS at all**, and 4 of its
8 attempt payloads were silently dropped. The failures are logged as
`WARNING`, not fatal — `RACE_LEDGER: failed to write ledger rows: ...` — so
neither booking was affected; task 0's booking still won and was confirmed.
Everything in §2's task-0 table above was reconstructed from Cloud Run logs,
not from the GCS ledger, because the ledger doesn't exist there.

Attempts 5–8 survived because by then the two tasks' verdicts diverged (task
1 accepted at 5–7, task 0 refused at 5–7 then accepted at 8), giving them
different filenames and no collision.

This is the same collision class already solved once in this codebase, for a
different case: `app/observer/run.py::_run_id` disambiguates *overlapping
executions* of the observer job with `CLOUD_RUN_EXECUTION`. It was never
applied to the racer's own `_capture_race_ledger` when #184/#204 fanned the
racer out to multiple tasks *within one execution* — a different collision
(`CLOUD_RUN_EXECUTION` is identical for both tasks; only `CLOUD_RUN_TASK_INDEX`
tells them apart).

**Fixed in #205**, adding `_race_run_id()`, scoped by `CLOUD_RUN_TASK_INDEX`.

Not fatal this morning because both races won cleanly and logs alone were
enough to reconstruct the missing ledger. It would matter on a morning where
one of the two bookings lost: the ledger is the primary artifact this skill's
§4–§7 depend on to classify *why*, and for a losing task under this
collision, it would not exist.

## 5. Fix needed

Two, both already opened as PRs against this morning's findings:

- **#205** — scope the race ledger's GCS directory by `CLOUD_RUN_TASK_INDEX`
  so concurrent racer tasks stop colliding on artifact filenames.
- **#206** — `fetch_debug_artifacts.py logs` (and the `booking-postmortem`
  skill's own `gcloud` example) now query the `teetime` service and both
  Cloud Run jobs together, so a job-era morning is no longer invisible to a
  service-scoped query.

No change needed to the booking logic itself — both races were clean wins on
early rungs, well inside every timing budget on record.
