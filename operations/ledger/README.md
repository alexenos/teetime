# The ledger

The race report cycle appends rows here. The scoreboard is a rollup over these
files.

The reports in `operations/race-reports/` hold the analysis: what was
established, what was hypothesis, what the evidence did not settle. The ledger
holds the values used for computation. Computing a rate by re-reading
twenty-three markdown documents is slow and non-deterministic: two sessions can
read the same document and score it differently.

One row per event, appended, never rewritten. JSON Lines, so a row can be added
without reading or rewriting the file.

Two files, one per metric that needs history. Cost is read from billing rather
than accumulated here.

---

## `runs.jsonl` — one row per cycle run

Input to the automation streak. Appended on every run, including runs that
produced no other output — a morning with no booking scheduled is still a run,
and a clean one.

```json
{"cycle":"race-report","date":"2026-09-15","clean":false,
 "note":"reported no booking; two had succeeded. Log query scoped to the service, fixed in #206"}
```

| Field | Definition |
|---|---|
| `cycle` | matches a file in `operations/cycles/` |
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

---

## Write path

Rows are written to GCS, not committed to the repository:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<file>.jsonl
```

Two reasons:

1. A commit per run produces one commit per morning, on top of the report
   commit the cycle already makes, and a merge conflict whenever two cycles run
   concurrently.
2. This matches existing practice. `scripts/observer_observations.py` writes
   `observations.jsonl` into the GCS run directory for the same reasons.

The repository holds the schema. The bucket holds the data. A periodic snapshot
into the repository is possible but is not implemented.

Note that the cycle's standing authorization covers exactly one file under
`operations/race-reports/`. A GCS write is a data write outside that path and
does not widen it; committing ledger rows to the repository would.

## Current state

**Not implemented.** No cycle emits rows, no rollup reads them, and the files in
this directory are empty. The schemas above are the specification for that work.

## Backfill

Twenty-three reports exist in `operations/race-reports/`, covering 2026-08-13 to
2026-09-25, and are not represented here. They are fewer than the number of runs:
races are weekday-only, and a morning with no booking scheduled produces a run but
no report — 2026-09-23 and 2026-09-26 are two recent examples, a Wednesday with
nothing scheduled and a Saturday. The earliest, 2026-08-13, predates the cycle.

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
