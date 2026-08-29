# Booking post-mortem: 2026-08-28

**Target:** Friday 2026-09-04, 08:38 AM, 4 players, Northgate
**Window:** Friday 2026-08-28 06:30:00 CT (job fires 06:28)
**Outcome: won a slot, lost the requested one.** Four refusals across two slots,
then the club granted **08:45 AM** — the second fallback — at +5279ms.
`RESERVATION_CHECK` confirms `09/04/2026 08:45 AM - 08:53 AM RESERVED`.
**Code that ran:** `4116c21` (#164 docs; last behavioural change #163).

**This is 08-21 again, almost beat for beat** — same target, same fallback
walk, same slot lost, first grant in the same club-second. One Friday was a
data point; two is a pattern, and this morning had the instrumentation #161
built to read it.

---

## 1. Verdict

Attempts 1 and 2 were refused **because the sheet was still closed** — the
first morning the `sheetOpen` ledger field was live, and it read `False` on
both, with the club's clock stamping :01 and :02. The sheet opened somewhere in
**(+2138, +3780)ms** (attempt 2 answered closed by +2138; attempt 3, sent
+2768 with a 1012ms round trip, answered open). Attempts 3 (08:38) and 4
(08:30) were refused **on an open sheet**; attempt 5 (08:45) was granted at
:05 — the same club-second as 08-21's grant, though the two mornings' sheets
opened at different times.

The boundary has now moved twice: ≤:01 through 08-20, ~:02 on 08-21, ~:03
today. Roughly a second later each Friday.

### RACE_LEDGER

| # | slot | sent+ms | club sec | verdict | sheetOpen | RT ms | wall/cpu ms | cpu/wall | container ms |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 08:38 | 1005 | :01 | refused | **False** | 444 | 196/170 | 0.87 burned | 240 |
| 2 | 08:38 | 1786 | :02 | refused | **False** | 352 | 440/170 | **0.39 descheduled** | 480 |
| 3 | 08:38 | 2768 | :03 | refused | **True** | 1012 | 186/179 | 0.96 burned | 280 |
| 4 | 08:30 | 4441 | :04 | refused | **True** | 268 | 179/149 | 0.83 burned | 210 |
| 5 | 08:45 | 5279 | :05 | **accepted** | — | 490 | 15/19 | — | 20 |

Artifacts: `gs://…/walden/race/20260828_113007/`. Attempt 3's 1012ms round
trip — 2-4x its neighbours, right at the open moment — reads like the
stampede hitting the club's server as the sheet opens.

---

## 2. First morning of CPU accounting, and what it says

#160 proposed O2′ (wall vs CPU per attempt); #161 shipped it; this is its
first race. Three findings:

- **The CPU thief is real but intermittent.** Attempt 2 shows the descheduled
  signature outright: cpu/wall 0.39, and the container burned **310ms that was
  not this process** — consistent with Chrome, still resident on the tee sheet
  with live JS timers until 06:30:16. One attempt in four.
- **It is not the dominant cost.** The other refusals sit at 0.83–0.96 —
  cycles genuinely burned — and our own 149–179ms per refusal is still ~3x the
  ~54ms idle-container benchmark. That surplus points at a slow or throttled
  Cloud Run vCPU, not theft.
- **#161's telemetry deferral worked.** Post-response totals fell from 1903ms
  (08-21) to ~1016ms today across a five-attempt race.

---

## 3. The Friday pattern

Every non-Friday race on record is granted at club-second **:01**. Both
Fridays were refused at :01 on a provably closed sheet and saw no grant to
anyone before **:05**:

| morning | day | target | attempt 1 | first grant |
|---|---|---|---|---|
| 08-16 | Sun | 08:00 | accepted | :01 |
| 08-20 | Thu | 08:08 | accepted | :01 |
| 08-25 | Tue | 08:02 | accepted | :01 |
| 08-27 | Thu | 08:08 | accepted | :01 |
| 08-21 | **Fri** | 08:38 | refused (closed) | :05 |
| 08-28 | **Fri** | 08:38 | refused (closed) | :05 |

The `disable-div` marker is sheet-wide, so the lateness is a property of the
whole Friday sheet, not of our slot. (08-15, a Saturday, opened at :01 —
this is Friday-specific, not weekend-specific.)

**The member evidence.** The maintainer pulled the sheets for 08-28 and 09-04:
the 08:38 slot is held by the **identical foursome both Fridays**, while 08:30
and 08:45 turned over to different groups each week. So the target's rival is a
recurring group that wants exactly this slot — and it did not have to be faster
than the bot. Every ask we have ever made for 08:38 landed either on a closed
sheet or within ~1.6s of it opening, and both Fridays we walked off to the
fallback list at :03 while the first grant to anyone came at :05. Whoever was
still asking at :05 won. It was not us, by policy.

**Established:** closed at :01/:02, open by :03, no grant before :05, target
held by the same group both weeks, one attempt's CPU stolen by a co-resident
process. **Still hypothesis:** whether the :03–:05 refusals were a lingering
grant gate or genuine contention — our deterministic ladder confounds slot
and time, and a refusal's slot rows are echoed (established 08-21), so the
payloads cannot separate them.

---

## 4. The fix in this PR: hold the target until the sheet opens

The sweep ladder (#150) assumes the sheet opens at 06:30:01. On Fridays it
does not, and the ladder spends every target ask into the closed window and
then leaves. Moving the aim later would forfeit the four non-Friday mornings
that win outright at +1030ms (the 08-21 T4 reasoning still holds). The fix is
to make the *policy* boundary-shaped instead of chasing the boundary:

- **A refusal whose own markup renders the sheet closed spends nothing.** The
  same slot is re-asked immediately, paced only by the club's answer rate
  (~300–500ms a round trip), capped at `walden_reserve_hold_cap_ms` (8s) past
  the stated window. The hammer doubles as a boundary probe: every answer
  lands in the ledger with `sheetOpen` and the club's clock, so the open
  moment is bracketed to one round trip from now on.
- **The first open-sheet refusal starts the fallback walk** — and the target
  is re-asked *between* fallbacks (`walden_reserve_target_interleave`) rather
  than abandoned, because an open-sheet refusal before the first grant moment
  is not yet proof the slot is taken. Same-slot re-asks never consume the
  attempt budget; only distinct fallbacks do.
- One execution path everywhere. Non-Fridays run the same loop and are
  unaffected in practice: their sheet is open at :01, so the hold has nothing
  to do and attempt 1 wins as it has five mornings running. Ad-hoc bookings
  run it too - their sheet opened days ago, so the hold no-ops - which means a
  Tuesday-afternoon booking exercises the exact code Friday's race runs, the
  same reasoning `walden_adhoc_execute_delay_s` already encodes.

On this morning's timeline the policy would have re-asked 08:38 at roughly
:04 and :05–:06 — the seconds in which, both Fridays, grants started flowing.
Next Friday's ledger will show either the target granted on a late re-ask
(persistence was the answer) or the target still refused while a fallback
grants (the group is faster than one round trip, or specially held — at which
point the conversation is speed or strategy, with the open moment finally
pinned).

## 5. Round trips (§7b of the skill)

456–1012ms this morning; 08-25 ran 831, 08-27 ran 675. 08-16's 2935ms still
stands alone against nineteen values from 268–1012. `_RESERVE_TIMEOUT_S`
stays 3.0s.

## 6. Carried forward

- **Park or quiet Chrome during the race** — now evidence-backed (attempt 2's
  310ms theft); paired with a post-race sheet snapshot in a follow-up PR.
- **08-20 #2 — alert when a run never reaches `BATCH_JOB:`** — third morning
  carried.
- **The "828ms" comment** at `walden_http_booker.py` — still unreconciled;
  the ledger's quiet-day worst remains 956ms.
- **Own-CPU 3x surplus over the idle benchmark** — points at the Cloud Run
  CPU allocation; unexplained, now measurable every morning.
