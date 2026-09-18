# Booking post-mortem: 2026-09-17

**Targets:** three ad-hoc bookings, submitted by hand between 06:28 and 06:32
CT. No 6:30 race ran — nothing was scheduled for this morning.

| # | request | Reserve was to fire | died | how |
|---|---|---|---|---|
| 1 (`50a07cc9`) | @rongarner, Wed 2026-09-23 3:08 PM, 4 players | 06:30:42 | 06:38:04 | Chrome came up, hung on the login navigation, killed by chromedriver read timeouts |
| 2 (`90f271e5`) | @melgarner, Wed 2026-09-23 3:15 PM, 4 players | 06:31:58 | 06:31:54 | `SessionNotCreatedException: DevToolsActivePort file doesn't exist` |
| 3 (`fb13ddc5`) | @rongarner, Thu 2026-09-24 3:08 PM, 4 players | 06:34:08 | 06:34:47 | same `DevToolsActivePort` crash |

**Window:** none raced. The ad-hoc path fires Reserve 90s after confirmation
(`walden_adhoc_execute_delay_s`), so each row above has its own execute time.
**Code that ran:** `545ad6d` — "Shift observer snapshots to start 500ms before
the window, every morning (#210)", merged 2026-09-16 20:22 CT, i.e. the night
before.

**Status: diagnosed, with one limit unconfirmed.** All three bookings failed
before any Reserve reached the club. Nothing was reserved, and no race ledger
exists for the date. The proximate cause is established from the logs; which
container limit was actually hit is not, and cannot be from the artifacts this
account can read.

Three further faults surfaced in how the morning was *reported*, each of which
would have hidden a failure on a morning that mattered. The observer also
captured nothing, which means #210 has still never produced a snapshot.

---

## 1. There was no race, and that part is working as designed

The routine's first pass reported "no booking was scheduled" and stopped, which
was correct. Both jobs said so themselves:

```
11:27:27 RACER: task 2/4 - no unclaimed booking group due at 06:30 CT, nothing to race
```

— and the same from tasks 0, 1 and 3, all `exit(0)`. `walden/race/` has no
`20260917_*` directory. Per `booking-postmortem` §2 the empty `logs` result was
cross-checked against the GCS listing rather than trusted on its own; both
agree.

## 2. Three ad-hoc bookings, one browser

Timeline, CT. The service cold-started at 06:28:52 (`AUTOSCALING`), so booking 1
arrived into an empty container.

| time | event |
|---|---|
| 06:29:04 | "for @rongarner book 9/23 at 3:08p" received, target resolved to `8501282320` |
| 06:29:11 | "Yes" — `ADHOC_TIMED: Reserve fires at 06:30:42 CT (in 90s)` |
| 06:29:12 | `BATCH_BOOKING: === STARTING BATCH BOOKING === date=2026-09-23, sorted_times=['15:08']` |
| 06:30:13 | "for @melgarner book 9/23 at 3:15" received |
| 06:30:18 | Gemini `DeadlineExceeded: 504`, retried (see §5) |
| **06:30:21** | **`BATCH_BOOKING: Step 1 - Logging in` / `Navigating to login page...`** — booking 1's Chrome, 69s after its batch began |
| 06:30:28 | booking 2 confirmed, `Reserve fires at 06:31:58` |
| 06:31:54 | **booking 2 fails** — `DevToolsActivePort file doesn't exist`, raised at `_create_driver()` |
| 06:32:22 | `BATCH BOOKING COMPLETE - Closing driver` |
| 06:32:26 | "for @rongarner book 9/24 at 3:08p" received |
| 06:32:38 | booking 3 confirmed, `Reserve fires at 06:34:08` |
| 06:34:24 | `urllib3 Retrying (total=2) ... ReadTimeoutError(localhost:64371, read timeout=120)` on session `f04c4c31` |
| 06:34:47 | **booking 3 fails** — same `DevToolsActivePort` crash |
| 06:36:37 | `urllib3 Retrying (total=1)` on the same session |
| 06:36:44 | `Telegram request failed sending to chat -5588592546: ConnectTimeout` |
| 06:38:04 | **booking 1 fails** — `ReadTimeoutError` out of `_perform_login` → `driver.get(self.LOGIN_URL)` |
| 06:38:28 | last outbound Telegram message of the morning |

The shape is the whole story. Booking 1's Chrome is the only one that ever
started, and it was already labouring — 69 seconds from batch start to the
login step, on a container that had nothing else to do. Bookings 2 and 3 both
arrived while that browser was alive, and neither could start one:
`DevToolsActivePort file doesn't exist` is Chrome dying before it can write the
port file. Booking 1 then never completed its navigation; chromedriver stopped
answering, and three 120s read timeouts later it was declared failed, nine
minutes after the member was told it would "take a minute or two".

Note the ordering: the *first* request submitted was the *last* to fail, and
the two that failed first were the two that never got a browser at all.

## 3. Why: the container is sized for exactly one Chrome

Established from configuration, not inference:

- `cloud_run_max_instances = 1` (`terraform/terraform.tfvars.example`), so
  every webhook lands on the same container. No `container_concurrency` is set,
  so that one instance takes them all concurrently.
- `cloud_run_memory = "2Gi"`, and the variable's own description says why:
  "Headless Chrome peaks around 1 GiB on its own while a booking page with 150+
  slots is loaded, on top of the always-on FastAPI process… At 1Gi the
  container was OOM-killed mid-booking (2026-08-02)". The sizing assumes one
  browser.
- `cloud_run_cpu = "1"`.
- There is no lock, semaphore or queue in `booking_service.py` or
  `walden_provider.py` — `grep -n "Semaphore\|asyncio.Lock\|queue\|Queue"`
  returns nothing. Each ad-hoc booking is spawned as its own background task
  (`_spawn_bookings_batch_execution`) and goes straight to
  `asyncio.to_thread(_book_multiple_tee_times_sync)` → `_create_driver()`.

So three overlapping requests asked one 1-vCPU / 2Gi container for three
concurrent headless Chromes, against a budget written for one.

**What is *not* established: which limit was hit.** Memory exhaustion and CPU
starvation both produce this pair of symptoms, and `--disable-dev-shm-usage` is
already set so the classic `/dev/shm` cause is out. The post-mortem service
account is scoped to storage + logging (`docs/debug-artifact-access.md`), so
Cloud Run's memory and CPU metrics are not readable from here — the same IAM
gap §1 of the skill records for `run.revisions.list`. Distinguishing them needs
either `roles/monitoring.viewer` on that account or a reproduction under load.
It does not block the fix: serializing the sessions removes the contention
either way.

## 4. What the members actually received

Three separate faults, all in reporting rather than booking. Confirmed against
the member's own Telegram screenshots, not just the logs.

**The raw exception is sent verbatim.** `booking_service.py:1510` passes
`str(e)` into the notification, which lands in `_notify_booking_result` and
goes out unmodified. What arrived was `Message: session not created:
DevToolsActivePort file doesn't exist; For documentation on this error, please
visit: <url>`, followed by nineteen frames of `#0 0x559add5339f2 <unknown>`,
followed by a Telegram link-preview card for the Selenium docs.

**It is sent to the proxy target, not the requester.** These were proxy
bookings, so the failure notices are addressed `@melgarner` and `@rongarner`.
Result routing to the target is by design — the bot says so at confirmation
("Ronald will get the result") — but it means a third party who never touched
the bot received a chromedriver stack dump.

**One booking was never reported at all.** Mapping the three failures onto the
two messages the member actually received:

| failure | logged | delivered |
|---|---|---|
| booking 2, `DevToolsActivePort` | 06:31:54 | 06:32, to @melgarner ✓ |
| booking 3, `DevToolsActivePort` | 06:34:47 | **never** |
| booking 1, `ReadTimeout` | 06:38:04 | 06:38, to @rongarner ✓ |

Booking 3's notice is the `ConnectTimeout` at 06:36:44. `_try_notify_unreported_booking`
catches every exception, logs it and returns (`booking_service.py:1645`), so
the message was dropped and nothing retried. The screenshots at 06:46 confirm
it: the thread ends on "On it - booking Thursday, September 24… I'll message
you with the result", and no result ever came. To the member that is
indistinguishable from a booking still in progress — the same "looks like it
might still be fine" class as §6's chain-completed-no-reservation, one level
up.

All three are addressed in #211.

## 5. The observer captured nothing, and #210 has still never run

Per §7f this is a standing check, so it gets a section even though there was no
race to corroborate. **There are no observer artifacts for 2026-09-24.** The
prefix does not exist; the bucket's newest observer run is
`walden/observer/2026-09-23/20260916_112559_teetime-observer-xxk69/`, from
yesterday.

The job's own logs say why, and it is not the club:

```
11:26:05 OBSERVER: no booking due for any requester this morning - watching
         2026-09-24 anyway (today + 7 days), which is the sheet opening at the window
11:26:05 ERROR OBSERVER: no Walden login on file for any requester; nothing to
         log in with (WaldenCredentialRequiredError)
         Container called exit(1).
```

Read `_resolve_target` (`app/observer/run.py:63`) against that. It picks whose
login to watch with in three steps, as `config.py:502` describes them:

1. `observer_phone_number`, if set,
2. otherwise `user_phone_number`, if set,
3. otherwise whoever has the earliest booking due that morning — the empty
   `requester` makes `not requester` true, so the scoped list keeps every due
   booking and line 100 reassigns `requester = booking.phone_number`.

`_redacted` printed "any requester", so neither phone number is set in the
observer job's environment and **step 3 is what has been carrying every
successful run**: the observer borrows the credential of whoever had a booking
due. On a morning with none due there is no step 3 to reach,
`require_credentials("")` raises, and the job returns False — which `main()`
maps to `exit 1`.

**This was reviewed on the day and left as-is.** The maintainer's call: if no
tee time is scheduled to book at 06:30, the observer does not need to run, and
on the mornings that do have one it should behave identically every day. That
is what the code already does, so the `exit(1)` above is expected behaviour on
a quiet morning rather than a fault, and this section is a record of what the
job does, not a bug report.

Two things worth keeping straight when reading it:

- **The fallback branch is not dead code.** It computes `today +
  days_in_advance` and would run normally if `OBSERVER_PHONE_NUMBER` (or
  `USER_PHONE_NUMBER`) were ever set for the observer job, since that supplies
  a credential independent of the morning's bookings. It is inert today only
  because both are empty. An earlier draft of this document called the branch
  unreachable; that was wrong.
- **There is no Friday-specific behaviour anywhere in this codebase.** Every
  "Friday" in `walden_http_booker.py`, `walden_provider.py` and `config.py` is
  a comment explaining why a timing constant is sized as it is; the only code
  that branches on a weekday is `gemini_service.py` parsing a date out of a
  text message. The observer and the racer do the same thing every morning.
  §7f of the `booking-postmortem` skill discusses Fridays because that is when
  the club is contested — it describes the club's behaviour, not the bot's.

**This also means #210 has never produced a snapshot in production.** It merged
at 20:22 CT on 09-16, after that morning's run, so its first scheduled firing
was today's — the one that exited before capturing anything. Yesterday's run,
on pre-#210 code, shows the old cadence:

```
snapshot_+0000ms.html  +1378ms  +2529ms  +3596ms  +4546ms  +5613ms  +6635ms  +7535ms  +8443ms
```

Nine shots, ~1s apart, starting at the window. The configuration #210 shipped
is `observer_snapshot_start_offset_ms = -500`, `interval_ms = 1000`, `count =
9` — a 500ms *phase shift*, so the intended sequence is −500, +500, +1500 …
+7500. It is deliberately not a half-second cadence: the commit message notes
each snapshot costs a full click-and-re-render round trip (~1s), so tightening
the interval below that would make the wait a no-op without moving the capture.
**Unverified in production either way**, and it stays unverified until either a
booking morning arrives or the credential gap above is closed.

**Also noted, not chased:** Gemini returned `DeadlineExceeded: 504` on booking
2's parse at 06:30:18 and succeeded on retry 2.3s later. The retry did its job;
it is recorded only because a 504 inside the 06:28–06:31 window is the same
scarce-CPU story as everything else on this page, and a *third* retry failing
would have lost an already-typed booking.

## 6. Established vs hypothesis

**Established**

- No race ran; nothing was scheduled. Both jobs logged it, GCS agrees.
- All three ad-hoc bookings failed before any Reserve was sent. Two never got a
  browser; one never completed login. No `RESERVATION_CHECK`, no ledger, no
  reservation.
- The container is configured for one instance, one vCPU, 2Gi, with the memory
  figure explicitly sized around a single ~1GiB Chrome, and nothing in the code
  serializes browser sessions.
- Booking 3's failure was never reported to anyone; the notification raised
  `ConnectTimeout` and was swallowed.
- The observer captured nothing for 2026-09-24, because it has no credential to
  borrow on a morning with no due booking. Reviewed and kept: no booking, no
  observer (§5).

**Hypothesis**

- That the specific limit exceeded was memory rather than CPU. Symptoms fit
  both; metrics are unreadable from this account (§3).
- That booking 1's 69s login and the Gemini 504 share the same cause as the
  crashes. Plausible and consistent, not proven.

**Cheapest experiment that would discriminate**, and it costs no morning:
grant the post-mortem service account `roles/monitoring.viewer` and read the
container's memory utilization for 11:29–11:39 UTC on 2026-09-17. The data
already exists; only the permission is missing. Failing that, three concurrent
ad-hoc bookings against a deployed revision would reproduce it on demand —
the ad-hoc path needs no 6:30 window.

## 7. Follow-ups

- **#211** — serialize browser sessions behind one slot; trim the member-facing
  error; retry a dropped notification. Covers §3 and §4.
- **Closed, no change:** the observer's behaviour on a quiet morning (§5). A
  morning with nothing to book does not need an observer, and on the mornings
  that do have one the job already behaves identically every day, so the
  `exit(1)` stays.
- **Open, small:** that `exit(1)` makes Cloud Run record a *failed execution*
  on every quiet morning, for an outcome that is now the intended one. Nothing
  alerts on it today; if anything ever does, it will fire on exactly the
  mornings where nothing was supposed to happen. Returning 0 on the
  no-requester path would separate "declined, as designed" from "tried and
  broke".
- **Consequence to remember:** #210's shifted cadence is exercised only on
  mornings that have a booking. It has still never produced a snapshot; the
  first booking morning after 2026-09-16 will be its first real run, and is
  the one to check it on.
- **Open:** `roles/monitoring.viewer` on the post-mortem service account, which
  would have settled §3 in one query.
