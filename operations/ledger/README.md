# The ledger

Two tiers of row, which are different kinds of thing and are easy to conflate.

| Tier | File | Question it answers | One row per |
|---|---|---|---|
| **Events** | `mornings.jsonl`, `runs.jsonl` | what happened | thing that occurred |
| **Snapshots** | `snapshots.jsonl` | what the metrics read, and when | day |

Events are facts. A row states what happened on one morning and is never
revisited. Snapshots are measurements: each one is a reading of the scoreboard on
a date, under the metric definitions in force on that date. The scoreboard itself
is neither — it is the newest snapshot rendered for a reader, recomputed on demand
and never stored.

The reports in `operations/race-reports/` hold the analysis: what was
established, what was hypothesis, what the evidence did not settle. The ledger
holds the values used for computation. Computing a rate by re-reading
twenty-three markdown documents is slow and non-deterministic: two sessions can
read the same document and score it differently.

All rows are appended, never rewritten. JSON Lines, so a row can be added without
reading or rewriting the file.

---

## `runs.jsonl` — one row per Routine run

Input to the automation streak. Appended on every run, including runs that
produced no other output — a morning with no booking scheduled is still a run,
and a clean one.

```json
{"routine":"race-report","date":"2026-09-15","clean":false,
 "note":"reported no booking; two had succeeded. Log query scoped to the service, fixed in #206"}
```

| Field | Definition |
|---|---|
| `routine` | matches a file in `operations/routines/` |
| `date` | run date, CT |
| `clean` | whether the output required correction before use; independent of the outcome reported |
| `note` | required when `clean` is false; states what was wrong |

The streak is the count of `clean: true` rows from newest back to the first
`false`. Do not store the streak.

A row cannot always be written correctly at the end of the run that produced
it: the report from the 2026-09-18 run read as clean that morning and was
retracted the following day by #219. Amending a row contradicts append-only, so record the correction
as a later row for the same `date` and take the newest row for a date as
authoritative.

## `mornings.jsonl` — one row per booking request

Input to the outcome split.

```json
{"date":"2026-09-15","target_date":"2026-09-22","requester":"<id>",
 "requested":"08:10","booked":"08:10","outcome":"exact",
 "confirmed_slot":true,"source":"RESERVATION_CHECK"}
```

`outcome` is `exact`, `fallback` or `miss`, determined by `RESERVATION_CHECK`.

`confirmed_slot` records whether the requested time was resolved to a bookable
slot and confirmed with the member before the race (#216). It is `false` for
every row predating that feature. On such a row, `exact` means only that the
booked time matched the time requested, which may not have corresponded to an
available slot. Do not compare across that boundary without stating it.

`requester` is a member identifier. This repository is public; do not write
member names, phone numbers or Telegram handles into it.


## `snapshots.jsonl` — one row per day

The history behind the scoreboard. This is what a trend is plotted from.

```json
{"date":"2026-09-25","definitions":"v1",
 "outcome_split":{"window_mornings":4,"exact":null,"fallback":null,"miss":0,"not_miss":6},
 "automation_streak":{"routine":"race-report","clean_runs":7,"since":"2026-09-19"},
 "cost":{"month":"2026-09","usd_total":null,"usd_per_booking":null,"source":"unavailable"}}
```

| Field | Definition |
|---|---|
| `date` | the date the snapshot was taken, CT |
| `definitions` | which version of the metric definitions this row was computed under |
| each metric | the value as read that day, or `null` where the source was unavailable |

**Appended every day, including days nothing changed.** An identical consecutive
row is not waste: it is the only thing that distinguishes "the metrics did not
move" from "nothing ran". A gap in dates means a run was missed.

**`null` is a value.** A metric whose source does not exist records `null` with
the reason, not a zero and not an omitted key. Cost is `null` on every row until
a billing export exists; an omitted key would later be indistinguishable from a
month that cost nothing.

### `definitions` is the field that makes this worth storing

A snapshot could otherwise be recomputed from the events, and this file would be
a cache. It is not, for two reasons.

**Definitions change under you.** #216 will change what Exact means. Recomputing
2026-09-15 next month under next month's definition yields a number that was
never true, and puts a discontinuity in the trend that is not a change in
performance. Bump `definitions` whenever a metric's meaning changes, and never
compare rows across versions without saying so. The version is a label, not a
date: `v1` is the definitions as of this file.

**Some values cannot be recomputed at all.** Month-to-date cost is not
reconstructable after the fact, and agent and token spend is not queryable
historically. For those, the snapshot is the only record there will ever be.

---

## Write path

Rows are written to GCS, not committed to the repository:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<file>.jsonl
```

Two reasons:

1. A commit per run produces one commit per morning, on top of the report
   commit the Routine already makes, and a merge conflict if two runs overlap.
2. This matches existing practice. `scripts/observer_observations.py` writes
   `observations.jsonl` into the GCS run directory for the same reasons.

The repository holds the schema. The bucket holds the data. A periodic snapshot
into the repository is possible but is not implemented.

Note that the Routine's standing authorization covers exactly one file under
`operations/race-reports/`. A GCS write is a data write outside that path and
does not widen it; committing ledger rows to the repository would.

## Current state

**Not implemented.** Nothing emits rows, no rollup reads them, and the files in
this directory are empty. The schemas above are the specification for that work.

## Backfill

Twenty-three reports exist in `operations/race-reports/`, covering 2026-08-13 to
2026-09-25, and are not represented here. They are fewer than the number of runs:
races are weekday-only, and a morning with no booking scheduled produces a run but
no report — 2026-09-23 and 2026-09-26 are two recent examples, a Wednesday with
nothing scheduled and a Saturday. The earliest, 2026-08-13, predates the Routine.

They are not backfilled, for two reasons:

- `outcome` under the current definition depends on `confirmed_slot`, which is
  false for all of them. Every historical row would require the qualification
  above to be interpretable.
- `clean` for a past run is an assessment of whether its output required
  correction. The documents do not record this directly. 2026-09-15 and
  2026-09-18 are determinable because the corrections are written down. Most are
  not.

Backfill from the documents individually where there is a reason to, marking any
field that was inferred rather than read. A missing row is detectable; an
inferred row presented as read is not.
