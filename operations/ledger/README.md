# The ledger

Cycles append rows here. The scoreboard is a rollup over these files.

The post-mortem documents in `docs/` hold the analysis: what was established,
what was hypothesis, what the evidence did not settle. The ledger holds the
values used for computation. Computing a rate by re-reading nineteen markdown
documents is slow and non-deterministic: two sessions can read the same
document and score it differently.

One row per event, appended, never rewritten. JSON Lines, so a row can be
added without reading or rewriting the file.

---

## `runs.jsonl` — one row per cycle run

Input to the autonomy streak. Every cycle appends on every run, including runs
that produced no other output.

```json
{"cycle":"morning-postmortem","date":"2026-09-15","rung":0,"clean":false,
 "note":"reported no booking; two had succeeded. Log query scoped to the service, fixed in #206"}
```

| Field | Definition |
|---|---|
| `cycle` | matches a file in `operations/cycles/` |
| `date` | run date, CT |
| `rung` | rung operated at, 0–5 |
| `clean` | whether the output required correction before use; independent of the outcome reported |
| `note` | required when `clean` is false; states what was wrong |

The streak is the count of `clean: true` rows from newest back to the first
`false`. Do not store the streak.

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

## `changes.jsonl` — one row per merged PR

Input to fix latency.

```json
{"pr":206,"identified":"2026-09-15","merged":"2026-09-16",
 "source":"docs/booking-post-mortem-2026-09-15.md","human_edited":false}
```

`identified` is the date a post-mortem or review named the fix. It is not the
date the defect was introduced or first observed.

`human_edited` supports human-touch rate if that metric is adopted. Record it
regardless, as it cannot be reconstructed later.

---

## Write path

Rows are written to GCS, not committed to the repository:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<file>.jsonl
```

Three reasons:

1. `operations/autonomy.md` places the post-mortem cycle at R0, which permits
   no repository writes. A cycle at R0 cannot commit a row. Writing telemetry
   to the artifact bucket is a data write and does not require a rung change.
2. A commit per run produces one commit per morning and a merge conflict
   whenever two cycles run concurrently.
3. This matches existing practice. `scripts/observer_observations.py` writes
   `observations.jsonl` into the GCS run directory for the same reasons.

The repository holds the schema. The bucket holds the data. A periodic
snapshot into the repository is possible but is not currently implemented.

## Current state

**Not implemented.** No cycle emits rows, no rollup reads them, and the files
in this directory are empty. The schemas above are the specification for that
work.

## Backfill

Nineteen mornings of history exist in `docs/` and are not represented here.

They are not backfilled, for two reasons:

- `outcome` under the current definition depends on `confirmed_slot`, which is
  false for all of them. Every historical row would require the qualification
  above to be interpretable.
- `clean` for a past run is an assessment of whether its output required
  correction. The documents do not record this directly. 2026-09-15 is
  determinable because the correction is written down. Most are not.

Backfill from the documents individually where there is a reason to, marking
any field that was inferred rather than read. A missing row is detectable; an
inferred row presented as read is not.
