# The ledgers

**One ledger per Routine, named for the Routine.** Every row answers two things:
did that run succeed, and what did it measure.

| File | Routine | Owns |
|---|---|---|
| `race-report.jsonl` | race report | the day's outcome split |
| `scoreboard.jsonl` | scoreboard | the derived metrics, as published |
| `cost.jsonl` | cost | the month's spend |

A Routine writes only its own ledger. The scoreboard Routine is the only one that
*reads* the others, and it reads them to derive; it never writes to them.

Rows are appended, never rewritten. JSON Lines, so a row can be added without
reading or rewriting the file.

The reports in `operations/race-reports/` hold the analysis: what was established,
what was hypothesis, what the evidence did not settle. The ledgers hold the values
used for computation. Computing a rate by re-reading twenty-three markdown
documents is slow and non-deterministic: two sessions can read the same document
and score it differently.

---

## Every row carries the run

Whatever else a ledger holds, every row starts with the same four fields. This is
what makes "did the automation run" answerable without reading anything else.

| Field | Definition |
|---|---|
| `date` | run date, CT |
| `routine` | matches a file in `operations/routines/` |
| `ok` | whether the run did its job |
| `note` | required when `ok` is false; states what went wrong |

`ok` is about the run, not the outcome it reported. A race report that correctly
diagnoses a lost morning is `ok: true`. A race report that misdiagnoses the
morning is `ok: false` even though it produced a file — 2026-09-15 reported "no
booking" on a morning when two bookings succeeded.

**A missing date is a missed run.** No row for a date the Routine should have
fired on is the signal; there is no "did not run" row to write, because a Routine
that did not run cannot write one.

## `race-report.jsonl`

One row per morning the Routine fires. Day-level counts, not a rolling average —
the rolling windows are the scoreboard's job.

From the morning of 2026-09-25: two requests, one granted the target on the first
Reserve and one that lost 08:38 and took 08:45 on the eighth.

**The times and outcomes are read from that morning's report. The two `member`
values are invented** — no salt exists yet, so no real identifier has ever been
computed. They are shaped like the real thing and are not the real thing.

```json
{"date":"2026-09-25","routine":"race-report","ok":true,
 "raced":true,"report":"operations/race-reports/2026-09-25.md","pr":225,
 "requests":[
   {"member":"m_7b2e04","requested":"08:38","booked":"08:45","outcome":"fallback"},
   {"member":"m_3f9a1c","requested":"09:23","booked":"09:23","outcome":"exact"}
 ],
 "outcome":{"exact":1,"fallback":1,"miss":0},
 "confirmed_slots":false}
```

| Field | Definition |
|---|---|
| `raced` | false on a morning with no booking scheduled. A clean run with no report. |
| `report` | path to the published report, or `null` when `raced` is false |
| `pr` | the report's PR number, or `null` |
| `requests` | one object per booking request. Omitted when `raced` is false. |
| `outcome` | the day's totals, by `RESERVATION_CHECK`. Omitted when `raced` is false. |
| `confirmed_slots` | whether the requested times were confirmed with the member before the race (#216) |

### `requests` — one object per request

| Field | Definition |
|---|---|
| `member` | an opaque member identifier; see below |
| `requested` | the tee time asked for, CT |
| `booked` | the tee time reserved, or `null` on a miss |
| `outcome` | `exact`, `fallback` or `miss`, by `RESERVATION_CHECK` |

**Both the detail and the totals are recorded**, even though the totals are a
rollup of the detail. The totals are what the scoreboard reads, and keeping them
explicit means a row can be checked against itself: `outcome` must equal the
rollup of `requests`. A row where they disagree is wrong, and that is detectable
without recomputation.

Requested against booked is the pair that makes a fallback legible. `08:38 → 08:45`
says the member was moved seven minutes; a bucket label alone does not. It is also
what will make #216 measurable — once requests are confirmed against a real slot,
the distance between requested and booked becomes a number worth trending rather
than a consequence of asking for a time that never existed.

### `member` is opaque, and deliberately so

The requester identity in the application is a phone number
(`app/models/database.py`, `phone_number` on `BookingRecord`, `SessionRecord` and
`WaldenCredential`; the same field carries a Discord snowflake or Telegram user id
for those channels). All three are personal data.

`member` is therefore a stable opaque identifier — a salted hash prefix, with the
salt held in Secret Manager — and **the mapping from it to a person is not in this
repository.** Resolve it against the database when a question actually needs a
name.

Two reasons it is salted rather than a plain hash. A phone number has around ten
digits of entropy, so an unsalted hash is trivially reversible by enumeration. And
these rows feed a page served publicly from `docs/`; an identifier that is
reversible is personal data wherever it ends up.

**`member` must never reach the page.** It exists so that "is one member
consistently getting fallbacks" is answerable from the ledger. The scoreboard
publishes counts, and counts only.

**`confirmed_slots` is what keeps a trend honest.** Until #216 ships it is `false`,
and on such a row `exact` means only that the booked time matched the time
requested — which may not have corresponded to an available slot. When #216 lands
the field flips to `true` and `exact` starts meaning something stricter. A trend
drawn across that boundary shows a step that is not a change in performance. The
scoreboard must not compare across it without saying so.

## `scoreboard.jsonl`

One row per scoreboard run: the metrics as published, at the moment they were
published. This is the history a trend is plotted from.

```json
{"date":"2026-09-25","routine":"scoreboard","ok":true,
 "published":"docs/scoreboard.json",
 "outcome_all_time":{"exact":0,"fallback":0,"miss":0,"confirmed_slots":false},
 "outcome_4wk":{"exact":0,"fallback":0,"miss":0,"confirmed_slots":false},
 "streak":{"consecutive":0,"by_routine":{},"total_ok":0},
 "cost":{"month":"2026-09","usd_total":null,"usd_per_booking":null,"source":"unavailable"}}
```

Written every run, including runs where nothing moved. An identical consecutive
row is not waste: it is the only thing separating "the metrics did not move" from
"nothing ran".

**`null` is a value.** A metric whose source does not exist records `null` with a
reason, not a zero and not an omitted key. Cost is `null` on every row until a
billing export exists; an omitted key would later be indistinguishable from a
month that cost nothing.

## `cost.jsonl`

One row per cost run. Monthly, so most days have no row.

```json
{"date":"2026-10-01","routine":"cost","ok":true,
 "month":"2026-09","usd_gcp":null,"scope":"gcp_only",
 "source":"bigquery:<dataset>"}
```

`scope` states what the figure covers, because it does not cover everything. GCP
spend only, per #227. Whether Anthropic agent and token spend can be measured at
all is open, in #228; if it becomes available, `scope` changes and a `usd_agent`
field joins the row rather than being folded silently into the total.

A cost number whose scope is unstated is worse than no number, because it invites
$/booking comparisons against a denominator that does not match it.

---

## Write path

Rows are written to GCS, not committed to the repository:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<file>.jsonl
```

Two reasons: a commit per run adds one commit per morning on top of the report
commit the race report already makes, and `scripts/observer_observations.py`
already writes its ledger to GCS for the same reason.

The repository holds the schema. The bucket holds the data. The files in this
directory are empty and exist to make the set visible.

A GCS write is outside the race report Routine's authorized repository path and
does not widen it; committing rows would.

## Current state

**Not implemented.** Nothing emits rows, nothing reads them, and the files here
are empty. The schemas above are the specification for that work.

## Backfill

Twenty-three reports exist in `operations/race-reports/`, covering 2026-08-13 to
2026-09-25, and are not represented here. They are fewer than the number of runs:
races are weekday-only, and a morning with no booking produces a run but no
report — 2026-09-23 and 2026-09-26 are recent examples. The earliest, 2026-08-13,
predates the Routine.

`outcome` is backfillable from each report, since the verdict and the slot count
are stated in it. `ok` is not: it is an assessment of whether the output needed
correction, which the documents do not record. 2026-09-15 and 2026-09-18 are
determinable because the corrections are written down; most are not.

Backfill individually where there is a reason to, marking any field that was
inferred rather than read. A missing row is detectable; an inferred row presented
as read is not.
