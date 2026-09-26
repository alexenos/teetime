# Design: a variable burst around a measured gate

**Status:** implemented (2026-09-25), not yet deployed. Replaces the six-member
burst aimed at +1030.
**Companions:** `operations/race-reports/2026-09-25.md`,
`operations/design-observer-and-fanout.md`

Every number here is **milliseconds past 06:30:00.000 CT**, the frame the race
ledger and the terraform settings use.

---

## Why

The Friday 08:38 slot is decided by tens of milliseconds between us and one other
automated client (Letbetter's foursome: 08:38 on three of the four Friday postrace
sheets, and their fallback 08:23 on the fourth - the morning we won 08:38). Of
the three burst-era Fridays we won 08:38 once, on 09-18.

Two findings from the raw ledgers, logs and code shaped the change:

1. **Every burst member dialled a new connection at fire time.** The HTTP client
   used httpx's defaults, which expire an idle connection after 5 s, and
   staging's last request ends 66-95 s before the window. So each member paid a
   TCP and TLS 1.3 handshake (~55-60 ms from Cloud Run) before its bytes left,
   and the ledger's `sentMsPastWindow`, stamped before the HTTP call, never
   showed it. On 09-25 the 08:38 ask logged at +1015 left at about +1070. The one
   exception was **09-18**: the racer started late, its clock probe ended under
   a second before the window, and both first asks rode that still-open
   connection. It is the only burst-era Friday we won 08:38.
2. **The deployed aim was +1030, not +1000.** #173 set the margin to 0 in
   `app/config.py`; terraform kept 30, and terraform is what deploys.

And one about the gate itself: nothing had ever been written to the wire between
+817 (refused, 08-12) and about +1014 (granted, 09-18), so where the gate opens
inside that span had never been measured, on any weekday.

## The plan

**Gate** = `walden_window_opens_offset_ms` = **+1000**.
**Aim** = gate + `walden_reserve_aim_margin_ms` (5) = **+1005**. The 5 ms exists
only to be after the gate.

The burst is one plan, every weekday, every task:

| part | members | spacing | purpose |
|---|---|---|---|
| +815 .. +955 | 8 | 20 ms | search: where does the gate really open? |
| +975 .. +1035 | 13 | 5 ms | the race: some member lands within 5 ms after a gate anywhere in here |
| +1055 .. +1175 | 7 | 20 ms | a gate later than the dense part |

28 members, all for the target. The last member is +1175 because 20 ms steps
from +1035 do not land on +1185. The serial fallback walk runs after the burst,
unchanged. Each member is sent at its instant minus the day's clock-offset lead,
as before.

The shape is five settings around the aim (`walden_burst_start_before_aim_ms`
190, `walden_burst_end_after_aim_ms` 180, `walden_burst_dense_half_width_ms` 30,
`walden_burst_dense_spacing_ms` 5, `walden_burst_sparse_spacing_ms` 20), so the
whole plan moves with the gate setting. `burst_plan_offsets_ms()` in
`app/config.py` turns them into the member list.

## Connections opened ahead of time

About 2 s before the first member, the booker opens one connection per member
(`PrimeFacesSession.prewarm`): concurrent HEADs on the static asset the clock
probe uses, each holding its connection until all of them have one, so a thread
that starts late cannot ride a connection another just released. The pool keeps
64 idle connections for 30 s. With under 250 ms left (staging ran late), the
pre-warm is skipped rather than delay the burst. `walden_burst_prewarm_connections`
turns it off.

## What every race now records

Per member, in `ledger.jsonl`:

| field | meaning |
|---|---|
| `planOffsetMs` | the member's place around the aim |
| `plannedSendMsPastWindow` | when it was due to leave, lead included |
| `wroteMsPastWindow` | when its bytes actually left (httpx trace) |
| `writeDriftMs` | wrote minus planned |
| `openedConnection`, `connectMs`, `localPort` | did it dial, how long that took, which connection |
| `responseHeadersMsPastWindow`, `leadMs` | first byte back, and the lead applied |

Per race, in `run.json` beside the ledger, and in three log lines:

- **`GATE_BRACKET:`** per slot: the gate opened after the last refusal and by
  the first grant, placed at write time plus lead. A bracket can be off by one
  spacing, because our own grant refuses our later asks while in flight and the
  club may reorder requests a few ms apart; read the distribution, not one
  morning. No grant means someone was faster than our first ask after the gate,
  or the gate was later than the burst.
- **`BURST_TIMING:`** warm versus dialled members, and write drift (median, p90,
  max, count later than 2 ms).
- **`BURST_CPU:`** usable CPUs and cgroup limit, then for the send window and the
  whole burst: container versus process CPU, run-queue delay of our own threads,
  CPU pressure (PSI) and throttled time.

## Reading it across days

```bash
poetry run python scripts/fetch_debug_artifacts.py gate --since 20260927
```

It prints each race's bracket, the intersection per weekday (where one gate
would sit, flagged when the brackets disagree), and each race's CPU and write
record. When the brackets settle, move `walden_window_opens_offset_ms` in
`terraform/variables.tf`; the whole burst re-centres on it.

## The second vCPU

`racer_cpu` is now 2. Whether it helps is read from `BURST_CPU` and
`BURST_TIMING`: run-queue delay, CPU pressure and throttling near zero, and write
drift within a millisecond or two, mean the CPU is not what makes members late.
To prove the second vCPU is the reason, run a morning or two with `racer_cpu = 1`
and the same plan, and compare the same fields.

## Risks worth watching

1. **Pre-gate asks might hold the slot briefly while being refused.** It would
   show as an uncontested morning where the member right after the bracketed
   gate is refused and a later one granted. Response: start the burst later.
2. **Footprint.** 28 Reserves per task, most returning a 550-700 KB refusal, in
   ~360 ms. No throttling has ever been seen (24 per morning on 09-18 and 09-19),
   and every 403, 429 or 503 is recorded per member.
3. **The gate may differ by slot** if the sheet unlocks row by row. Mornings with
   several tasks compare brackets for different slots.
4. **No race, no measurement.** Only a Reserve measures the Reserve gate - the
   observer's DOM loses the club's "not open yet" markers to the page's own
   scripts - so a weekday nobody books stays blank. No Monday has ever raced.
