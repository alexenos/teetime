# Booking post-mortem: 2026-08-21

**Target:** Friday 2026-08-28, 08:38 AM, 4 players, Northgate
**Window:** Friday 2026-08-21 06:30:00 CT (job fires 06:28)
**Outcome: won a slot, lost the requested one.** Four refusals across two slots,
then the club granted **08:45 AM** — the second entry on the fallback list — at
+4871ms. `RESERVATION_CHECK` confirms `08/28/2026 08:45 AM - 08:53 AM RESERVED`.
**Code that ran:** `b9b0106` (#157), merged 2026-08-20 14:57 CT. Nothing has
merged since, so `main` is what executed.

**This is the first morning the bot has been pushed off its requested slot since
#150.** It is also the first morning that produces a direct measurement of the
club's sheet-open moment, and the first that measures where the race budget
actually goes. Both are new, and both change what the next fix should be.

---

## 1. Verdict

The first Reserve was refused **because the club's sheet was still closed when it
arrived**, not because a human took the slot. The club's own reply to attempt 1
rendered the tee sheet as disabled. The open moment, which sat at or before
club-second 06:30:01 on the three previous races, had moved to 06:30:02 —
and the bot is aimed at +1030ms, which is 06:30:01. We arrived ~1s early into a
boundary that drifted, exactly the failure mode #150 was built to fix, against a
boundary one second later than #150 assumed.

The three refusals *after* that (08:38 at +2048 and +3243, 08:30 at +4009) landed
on an open sheet and are **not** explained by the boundary. Those are the ones
where the maintainer's "beaten by humans" hypothesis is live — and they are
unresolved, for reasons set out in §4.

---

## 2. The run in order

| Stage | What happened |
|---|---|
| Login → sheet | Steps 1-4 clean, 06:28:15 → 06:28:52 |
| Course select | Checkbox dropdown raised `element not interactable`; fell back to verifying the page was already Northgate. Recovered in ~7s, 97s of slack remained. First appearance in a post-mortem |
| Slot scan | 151 rows → 9 candidates (dropped course=64, window=78) |
| Pre-locate | 08:38 AM, `index=11`, `exact=True`, `available=4`, **8 fallbacks** (7 off-grid) |
| Clock | club **+1ms**, one-way 13ms, 105 probes, 5 transitions, **tick pinned to ±24ms** |
| Lead | 15ms early |
| Aim | +1030ms past the stated window |
| Fire | Reserve **1/8** at **+1015ms** |
| Ledger boundary | `club granted 08:45 AM at +4871ms; last refusal was +4009ms` |
| Chain | `phase=complete, success=True, blocked=False, attempts=5, totalMs=5919` |
| Verify | `RESERVATION_CHECK: Reservation found - 08/28/2026 08:45 AM - 08:53 AM RESERVED` |

The clock probe was excellent — 105 probes, 5 transitions, tick pinned to ±24ms,
club offset +1ms. **Nothing about this morning is a clock problem.** The aim was
executed to within 15ms of intent. The target it was aiming at had moved.

### RACE_LEDGER

| # | slot | sent+ms | club sec | verdict | roundTripMs | bytes |
|---|---|---|---|---|---|---|
| 1 | 08:38 AM | 1015 | 06:30:01 | refused | 465 | 670,975 |
| 2 | 08:38 AM | 2048 | 06:30:02 | refused | 485 | 670,775 |
| 3 | 08:38 AM | 3243 | 06:30:03 | refused | 314 | 670,775 |
| 4 | 08:30 AM | 4009 | 06:30:04 | refused | 672 | 670,775 |
| 5 | 08:45 AM | 4871 | 06:30:05 | **accepted** | 395 | 86,151 |

All four refusals carry `PF('teeSheetValidationErrorPopupVar').show();;`; the
accept carries `executeHoldTimeTimer('300');;stopSheetTimers();;…`. Verdicts
reproduced exactly with `classify_reserve_response()` against the stored
envelopes.

Artifacts: `gs://…/walden/race/20260821_113006/`

---

## 3. Timing, in detail

This section is the point of the document. Prior post-mortems recorded *whether*
we were on time; this one records *where the time went*.

### 3a. The first request — did we lose it to a human?

**No. We lost it to the boundary.** Reconstructed from log timestamps
(stated window 06:30:00.000 CT = 11:30:00.000 UTC):

| Moment | Clock (CT) | ms past stated window |
|---|---|---|
| Job fires | 06:28:05.331 | −114,669 |
| Login complete | 06:28:33.475 | −86,525 |
| Sheet staged, 700,334 bytes held | 06:28:53.275 | −66,725 |
| Slot pre-located (`exact=True`) | 06:28:53.337 | −66,663 |
| Reserve staged, 1835 body bytes | 06:28:55.155 | −64,845 |
| Connection warmed (HTTP 200) | 06:28:56.002 | −63,998 |
| Clock probe complete (105 probes) | 06:29:01.047 | −58,953 |
| Precision sleep to first rung | — | → +1030 aim, less 15ms lead |
| **Reserve 1 fired** | **06:30:01.014** | **+1015** |
| Club stamps its `Date` | 06:30:01 (whole second) | — |
| Full body in hand | 06:30:01.479 | +1479 (roundTrip 465ms) |
| Verdict logged | 06:30:02.042 | +2042 |

Everything upstream of the fire was early and clean: the slot was located 66
seconds ahead, the request was staged 64 seconds ahead, the connection was warm,
and the clock was pinned to ±24ms. The bot fired 15ms before its aim point, as
designed.

**The decisive evidence is in the refusal payload itself.** Diffing attempt 1
(+1015ms) against attempt 4 (+4009ms) — 670KB each — yields exactly **two**
changed hunks in the entire document:

```diff
- <div id="…:bookingStartIn" class="booking-starts-in">Booking Starts In : 00:01:08</div>
- <div id="…:teeTimeSlots" class="ui-datascroller ui-widget disable-div">
+ <div id="…:teeTimeSlots" class="ui-datascroller ui-widget ">
```

The `disable-div` class is the club's own "this sheet is not open for booking"
marker. **At attempt 1 the club rendered the sheet disabled. By attempt 2 it did
not.** Cross-checked against 08-15's attempt 1, which was sent at −60ms — before
the window by anyone's reckoning — and which carries the same `disable-div` and
countdown pair:

| morning | first Reserve | club second | sheet in reply | verdict |
|---|---|---|---|---|
| 08-15 #1 | −60ms | 06:30:00 | **disabled** | refused |
| 08-15 #2 | +1240ms | 06:30:01 | (booking form) | accepted |
| 08-16 #1 | +1023ms | unbracketable (2935ms RT) | (booking form) | accepted |
| 08-20 #1 | +1006ms | 06:30:01 | (booking form) | accepted |
| **08-21 #1** | **+1015ms** | **06:30:01** | **disabled** | **refused** |
| 08-21 #2 | +2048ms | 06:30:02 | enabled | refused |

On 08-20 the club **accepted** a Reserve while its own clock read 06:30:01. On
08-21, at the same club-second, it rendered the sheet **closed**. That is a
direct, artifact-backed measurement of the open moment moving roughly one second
later, and it is the first time the boundary has been observed to drift at all —
§7a of the skill listed "whether the boundary is fixed or drifts" as the open
question. **It drifts.**

The countdown text (`00:01:08` ≈ 68s, matching the 06:28:53 staging) is the
frozen pre-window value, consistent with the established staleness finding. It is
the `disable-div` class, not the countdown, that carries the signal.

### 3b. The fallback chain — where the 4.28 seconds went

Fire-to-accept was 4280ms. Broken down per attempt, from log timestamps
(`post-response` = verdict logged − body in hand):

| # | slot | fired (+ms) | roundTrip (ms) | post-response (ms) | verdict at (+ms) | gap → next fire (ms) |
|---|---|---|---|---|---|---|
| 1 | 08:38 | 1015 | 465 | 564 | 2042 | 5.2 |
| 2 | 08:38 | 2048 | 485 | 704 | 3236 | 5.3 |
| 3 | 08:38 | 3243 | 314 | 448 | 4004 | 3.9 |
| 4 | 08:30 | 4009 | 672 | 187 | 4866 | 2.7 |
| 5 | 08:45 | 4871 | 395 | 29 | 5293 | — |

Totals across the race:

| Component | ms | share |
|---|---|---|
| Network + body download (`roundTripMs`) | 2278 | 53% |
| **Post-response processing** | **1903** | **44%** |
| Ladder scheduling | 17 | 0.4% |
| **Total** | **4280** | |

Three things follow, and all three are new.

**(i) The configured ladder spacing did nothing.** `sweep=0+250+1000ms` should put
rungs at +1030 / +1280 / +2030. They landed at +1015 / +2048 / +3243. Every rung's
instant had already passed by the time the previous answer arrived, so
`sleep_until` no-opped on all of them — behaviour the code documents at
`walden_http_booker.py:960-966`. The inter-attempt gaps were **3–5ms**. The ladder
is not pacing this race; the round trip and the post-response work are. Tuning
`walden_reserve_sweep_offsets_ms` would have changed nothing this morning.

**(ii) 44% of the race was spent on work that costs ~54ms.** `roundTripMs` is
stamped after `client.post()` returns, which for non-streaming httpx is after the
full body is read (`walden_http.py:1114`) — so body download is already inside the
465ms, and the extra 564ms is CPU-bound work downstream of it. Benchmarked in this
session against the same 670,975-byte payload:

| Step | Idle-container cost |
|---|---|
| `parse_partial_response` (envelope → markup + eval) | 1.5 ms |
| `parse_html` (full 670KB DOM) | 36.9 ms |
| `observe_reserve_response` (ledger fields) | 11.1 ms |
| `_relocate_reserve_in` (re-stage) | 0.9 ms |
| **Full post-response path** | **~54 ms** |

Production spent 187–704ms on it — a **3.5×–13× slowdown**. The accepted response
(86KB) took 29ms, so the cost scales with payload size, but not linearly enough to
be download: it is inside the CPU-bound segment. The code's own note at
`walden_http_booker.py:1141` estimates "~190ms for a 500KB sheet"; today's range
was 187–704ms on 670KB.

**(iii) Re-staging bought nothing.** `_relocate_reserve_in` re-stages against the
returned sheet each round. The ViewState was `3ae081c1` on all five attempts, and
the slot rows were byte-identical across the whole race — so every re-stage
produced the same request it already had.

Had post-response cost been the ~54ms it should be, attempt 5 would have fired at
roughly **+3.0s instead of +4.87s**.

**One candidate cause tested and ruled out.** `received_at_ms` is stamped before
`parse_partial_response(http_response.text)`, and `http_response.text` decodes
670KB of bytes to `str` — with `charset_normalizer` installed, httpx runs
*charset detection* on that body whenever the response declares no charset, which
is a plausible way to lose hundreds of milliseconds outside the benchmark above
(the benchmark was handed an already-decoded string, so it never covered this
step). Measured against the real payload: `bytes.decode('utf-8')` is **0.03ms**
and full `charset_normalizer` detection is **4.07ms**. Neither explains the gap.
The decode is not the cause; do not spend another session on it.

So the ~54ms benchmark stands as essentially complete, and the gap is
**environmental rather than algorithmic** — nothing in the code path accounts for
it. Which of the two environmental worlds it is remains open; O2′ in §5 is the
instrument that settles it.

---

## 4. Established vs hypothesis

**Established, from artifacts:**

1. The club rendered the sheet **closed** (`disable-div` + countdown) in its reply
   to a Reserve sent at +1015ms and stamped 06:30:01, and **open** in its reply to
   one sent at +2048ms stamped 06:30:02.
2. On 08-20 the club **accepted** a Reserve stamped 06:30:01. So the open moment
   moved later between 08-20 and 08-21. **The boundary drifts.**
3. Attempt 1's refusal is therefore a boundary refusal, not a lost race.
4. 44% of the race (1903ms of 4280ms) was post-response processing; the same work
   costs ~54ms on an idle container.
5. The configured sweep offsets had no effect: all rungs fired immediately on the
   previous answer, gaps of 3–5ms.
6. The slot-row markup did not change once across the entire race — including the
   rows for 08:38 and 08:30 — so it is echoed, not live.

**Hypothesis, not established:**

- **Why attempts 2, 3 and 4 were refused.** These landed on an open sheet. Two
  stories fit: genuine contention (other members or another bot took 08:38 and
  08:30), or a lingering server-side gate that kept refusing for a few seconds
  after the sheet rendered as open.
- The refusal payloads **cannot** settle it. Point (6) means the 08:38 row still
  showing a Reserve button at +4009ms is stale markup, not evidence the slot was
  free. Do not read row or button counts as live sheet state — this morning is the
  proof that they are not.
- **The weak signal, stated as such:** attempt 4 asked for a *different* slot
  (08:30) at +4009ms and was refused, while attempt 5 asked for another different
  slot (08:45) 862ms later and was granted. A blanket time gate should have let
  08:30 through at +4009ms. That it did not leans toward slot-specific contention
  on the two earlier, more desirable times — which would mean the maintainer's
  "beaten by humans" hypothesis is right *for attempts 2–4*, though not for
  attempt 1. One morning, one data point, and the sheet is stale: this is a lean,
  not a finding.

**What would settle it:** the `sheetOpen` ledger field proposed in §5 (O1), read
across the next few races. If a morning shows refusals continuing while
`sheetOpen=true` on slots that a later attempt then wins, contention is real. If
refusals only ever occur while `sheetOpen=false`, the boundary explains
everything and the fallback ladder is doing unnecessary work. **This costs no
morning** — it is a field extracted from a payload already in hand.

---

## 5. Recommendations

Each is assessed for whether it would slow the race. **None of them do**; the two
with any measurable cost are 0.02ms and 0.40ms and can both run after the verdict
is returned.

### Timing

**T1 — Classify refusals from `<eval>` alone; defer the full parse until after the
chain.** The verdict is fully determined by the eval script
(`PF('teeSheetValidationErrorPopupVar').show()` vs `executeHoldTimeTimer(...)`),
which `parse_partial_response` yields in **~2ms** versus ~54ms for the full path —
verified this session against all three stored envelopes. Keep each refusal's
markup in memory (670KB × 4 ≈ 2.7MB) and build the ledger rows after the race, so
no field is lost.
*Race cost: strictly negative.* Saves ~48ms of work per refusal, which at today's
observed 3.5–13× starvation is **~190–620ms per refusal, ~1.5–1.9s over the
chain**. Answerable offline against `tests/fixtures/reserve_responses/` — no
morning required. **This is the highest-value change on the page.**

**T2 — Park the browser before the race.** *Candidate fix for one hypothesis. Not
a diagnostic, and not yet actionable — see O2′.* If the 3.5–13× gap turns out to
be CPU contention, the leading suspect is that Chrome stays resident on the tee
sheet page throughout the race: the driver is not closed until 06:30:16, and the
accept eval calls `stopSheetTimers()`, so that page is running live JS timers
while the race is in flight. The fix would be to navigate the browser to
`about:blank` once the HTTP session has adopted its cookies at 06:28:53 (session
and cookies survive; `RESERVATION_CHECK` navigates to the reservations page
later anyway).
*Race cost: zero* — it would run ~67s before the window.
**But this explains nothing on its own.** Shipping it and watching the number
move is not a diagnosis; it is a guess with a deploy attached. It becomes
actionable only if O2′ reports the descheduled pattern *and* its `/proc` delta
points at a co-resident process. Until then it is a hypothesis, listed here so
the next post-mortem does not have to rediscover it.

**T3 — Consider advancing off a slot sooner once the sheet is confirmed open.**
*Hypothesis, do not implement yet.* Rungs 2 and 3 re-asked 08:38 at +2048 and
+3243 on an open sheet and both refused; the fallback list was not reached until
+4009ms.
*Race cost: negative for fallbacks, but a real risk to the primary slot* if a
refusal is a transient lock rather than a taken slot. Today's artifacts cannot
tell those apart (§4), so this waits on O1 data across several mornings.

**T4 — Do not raise the aim to +2030ms.** Today's boundary was late, but 08-16 and
08-20 won on attempt 1 at +1023 and +1006. Chasing one morning would forfeit the
mornings that currently win outright. The better answer to a drifting boundary is
T1: make the cycle cheap enough that rungs 2 and 3 land at ~+1.3s and ~+1.6s
instead of +2.0s and +3.2s, bracketing the drift without giving up the early aim.
T1 is worth doing in either world O2′ reports — if we are starved, cutting 54ms
of work to ~2ms cuts the starved multiple with it; if the CPU is merely slow, it
cuts the cost directly.
*Race cost: none — this is a decision not to change.*

### Observability

**O1 — `sheetOpen` (or `sheetDisabled`) on every ledger row.** The decisive
evidence this morning, and it took hand-diffing two 670KB XML files to find.
Implementation is `'disable-div' in markup` on a string already in memory.
*Measured cost: **0.02ms**.* Effectively free, and it is the field that settles
§4's open question.

**O2′ — Record post-response *wall time and CPU time* per attempt.** Not just the
elapsed span: the pair. `roundTripMs` hides this segment entirely because it
stops when the body lands, and elapsed time alone only restates the mystery —
it says the work took 564ms without saying why.

The ratio is what discriminates, and it settles the question in one morning:

| observed | cpu/wall | meaning | fix that follows |
|---|---|---|---|
| wall ~564ms, cpu ~550ms | **≈1.0** | we genuinely burned the cycles — the Cloud Run vCPU is far slower than a dev container, or the work is larger than benchmarked | reduce the work (T1), or raise the CPU allocation |
| wall ~564ms, cpu ~54ms | **≈0.1** | we were **descheduled** for half a second — something took the CPU away | find the thief, which is where T2 becomes actionable |

Add a `/proc/stat` total-CPU delta over the same span and the second half falls
out too: if the container burned far more CPU than this process did, a
co-resident process — Chrome — is the thief, which implicates T2 directly rather
than by hand-waving.

*Measured cost: `time.process_time()` **0.33µs** per call, `/proc/stat` read
**13µs**.* Effectively free.

**This, not T2, is the thing that explains the 44%.** One caveat kept honest: it
tells us which of the two worlds we are in, not the whole answer. A cpu/wall of
≈1.0 would confirm the cycles were real but still leave *why the same work costs
10× there* open — most likely a throttled or slower vCPU, which would point at
the Cloud Run CPU setting rather than at any code in this repository.

**O3 — Fix the `Firing Reserve k/N … Nms past the window` label.** For attempt 1 it
prints the true window-frame offset; for attempts 2+ it prints
`perf_counter() - started`, elapsed since the chain began, while still saying
"past the window" (`walden_http_booker.py:810-831`). Attempt 3 logged **"2228ms
past the window"** when the ledger's truth is **3243ms**. A log-only reading of
this morning understates the whole tail of the race by ~1015ms.
*Cost: zero* — string formatting. This one actively misleads post-mortems and
should be fixed regardless of everything else on this page.

**O4 — Fingerprint every attempt, including payloads not uploaded.** The run stored
3 of 5 envelopes, dropping attempts 2 and 3 as "repeats of the same sheet." That
heuristic was correct but it is why the sheet-open transition can only be bounded
to (+1015, +2048) rather than pinned. Store `markupBytes` + `markupSha1` per row.
*Measured cost: **0.40ms** for sha1 over 670KB*, and it can run entirely after the
verdict is returned — effectively zero on the critical path.

**O5 — Record the club's raw `Date` header string** alongside the derived
`serverMsPastWindow`. *Cost: zero.*

**O6 — Reconcile `walden_http_booker.py:1141`** ("~190ms for a 500KB sheet") with
today's 187–704ms on 670KB. Same class as the outstanding "828ms" discrepancy at
lines 196/212, which is still unreconciled.
*Cost: zero* — comment only.

### Carried forward, still unimplemented

- **08-20 #2 — alert when a run never reaches `BATCH_JOB:` / `BATCH COMPLETE`.**
  No alerting found in `app/api/jobs.py`. Second morning carried.
- **08-20 #5 — the "828ms" comment** at `walden_http_booker.py:196,212`. The ledger
  table still has no 828 row and now spans 314–2935ms. Second morning carried.
- **08-20 #1 — `pool_pre_ping`** is now **done**, landed in #157.

### Not recommended

- **`_RESERVE_TIMEOUT_S`:** leave at 3.0s. Today's round trips were 314–672ms.
  08-16's 2935ms still stands alone against thirteen values from 314–956ms.
- **The course-dropdown fallback** (`element not interactable` → text verification):
  recovered in ~7s with 97s of slack. One occurrence, no cost. Watch, do not fix.

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
| **08-21** | **race** | **465 / 485 / 314 / 672 / 395** |

---

## 6. For the skill

Three things this morning establishes that `SKILL.md` should carry:

1. **`disable-div` on the `teeTimeSlots` datascroller is the club's sheet-open
   marker, and it is live.** It is the only reliable read of whether the club
   considered itself open at the moment it answered. §7a's open question — does the
   boundary drift — is answered: **it does**, by ~1s between 08-20 and 08-21.
2. **Slot rows in a refusal are echoed, not live.** 670KB byte-identical across
   4 seconds of an open window. `sheetRows` and `reserveButtons` say nothing about
   who holds a slot. This is a new trap in the same family as the countdown and the
   popup.
3. **The `Firing Reserve` log line is not in the window frame after attempt 1.**
   Read `sentMsPastWindow` from the ledger; the log understates later rungs by the
   first rung's offset.
4. **Diagnose an unexplained time gap by wall-versus-CPU, not by shipping a fix.**
   `cpu/wall ≈ 1.0` means the cycles were burned; `≈0.1` means the process was
   descheduled. Two counter reads, 0.33µs. Also record that the bytes→str decode
   is ruled out (0.03ms plain, 4.07ms with charset detection) so it is not
   re-tested.
