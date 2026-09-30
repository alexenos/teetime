# The ledgers

**One ledger per Routine, named for the Routine.** Every row answers two things:
did that run succeed, and what did it measure.

| Prefix | Routine | Owns |
|---|---|---|
| `operations/race-report/` | race report | the day's outcome split |
| `operations/scoreboard/` | scoreboard | the derived metrics, as published |
| `operations/cost/` | cost | the month's spend |

The `.jsonl` files in this directory are empty and exist to make the set visible
in the repository. The data is in GCS, one object per run. `backfill/` is the
exception; see Backfill below.

A Routine writes only its own ledger. The scoreboard Routine is the only one that
*reads* the others, and it reads them to derive; it never writes to them.

**One object per run, not one appended file.** A run writes
`operations/<routine>/<YYYY-MM-DD>.json` and never touches anything already
there.

That is forced by the grant, and the grant is the right one. The Routine service
account holds `roles/storage.objectCreator` on the `operations/` prefix
(`terraform/main.tf`), which permits creating an object and gives no way to
overwrite or delete one. Appending to a single `.jsonl` is a read-modify-write:
it replaces the object, which needs delete permission, which would also let a
session destroy the race artifacts stored beside these ledgers.

So the layout is a consequence of least privilege, and it buys two things beyond
that. Append-only becomes **enforced rather than conventional** — no run can
rewrite an earlier run's record, including its own. And two runs that overlap
cannot clobber each other, because they are writing different objects.

Reading is unaffected: the same account holds `objectViewer` bucket-wide, so a
reader lists the prefix and reads every object under it.

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
| `ok` | whether the run did its job. `null` only on a backfill row, which records no run |
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
| `outcome` | `exact`, `fallback` or `miss`, by `RESERVATION_CHECK`. Exactly one, always present; there is no fourth value and no unscored request. |

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
requested — which may not have corresponded to an available slot. With #216 each
booking's race log states what was agreed (`SLOT_AGREEMENT: ... agreed=sheet`,
`agreed=unchecked` or `agreed=none`), and the field is `true` only on a morning
where every request was `agreed=sheet`; on such a row `exact` means something
stricter. It does not flip once and stay flipped: a request for a date whose sheet
had not been read is `agreed=unchecked`, and its morning stays `false`. A trend
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
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<routine>/<YYYY-MM-DD>.json
```

A monthly Routine uses the month in place of the date; the rule is one object per
run, named so that listing the prefix sorts chronologically.

Two reasons it is GCS rather than the repository: a commit per run adds one
commit per morning on top of the report commit the race report already makes, and
`scripts/observer_observations.py` already writes its ledger to GCS for the same
reason.

The repository holds the schema. The bucket holds the data. The files in this
directory are empty and exist to make the set visible.

A GCS write is outside the race report Routine's authorized repository path and
does not widen it; committing rows would.

## Current state

**Backfilled; no Routine writes rows yet.** The 23 rows in
`backfill/race-report.jsonl` are the only race-report rows, and
`derive_scoreboard.py` is the only reader. The race report Routine starts writing
once its rewritten prompt is pasted into the trigger (#234).

## Backfill

`backfill/race-report.jsonl` holds one row per race-morning report in
`operations/race-reports/`, 2026-08-13 to 2026-09-27: 23 rows, 32 requests. It was
written on 2026-09-30 so the scoreboard has a history before the Routines have
produced one. It is committed rather than only uploaded because it was
transcribed by hand from prose, so it needs review like any other derived claim.
Once uploaded, the objects in GCS are the ledger and this file is their reviewed
source.

**A backfill row carries a `backfill` object, and that is how every reader tells
it apart.** Its fields are what a Routine row would have, plus:

| Field | Definition |
|---|---|
| `backfill.written` | the date the row was transcribed, not the date it describes |
| `backfill.basis` | what the outcome rests on: `reservation_check` (the report quotes one), `report_no_reserve` (the report establishes no Reserve reached the club), or `member_failure_notice` (only the failure message the member received) |

Every value on the row was **read from the report, not from logs.** No row was
re-derived from `RESERVATION_CHECK` in the artifacts. Where the report quotes a
`RESERVATION_CHECK` line the basis says so. 2026-08-13 is the one exception:
that report is still marked open and its artifacts were never read. The miss is
scored on the member-facing failure and the attempt-1 refusal in the 2026-09-11
report, and its `note` says so.

**`ok` is `null` on every backfill row.** `ok` assesses a Routine run. A backfill
row does not record a run: the reports up to 2026-09-19 were written in
maintainer sessions, and a Routine run that produced a report left no record of
its own. So `null` means "not a run", not "failed". The streak must skip these
rows. Counting them as failures would break it, and counting them as successes
would claim correctness nobody checked. 2026-09-18 was corrected after merge
(#219), and 2026-09-15's first pass misreported the morning before the published
report fixed it. Neither correction changed the outcome, and both are in `note`.

Scoring follows the schema mechanically: `exact` when the booked time equals the
requested one, `fallback` otherwise, `miss` when nothing was reserved.
`confirmed_slots` is `false` on every row, since all of them predate #216.

**Folded and omitted.** The 2026-09-04 evening run was ad-hoc, not a race. It is
the second request on the 2026-09-04 row, because the ledger holds one object per
date. 2026-09-17 had no race but three ad-hoc requests inside the window, all
Miss, and it is included because `operations/scoreboard.md` counts it. Mornings
that raced with no report of their own are **absent, not scored**: 08-14, 08-15,
08-16 and 08-22 have attempt-1 ledger entries quoted in later reports but no
outcome, and 08-18 (lost to a login timeout, per the 2026-08-20 report) has no
requested time on record. The race report Routine has fired daily since
2026-08-20, so a missing date before 2026-09-30 is a gap in the backfill, not a
missed run.

### Writing it to GCS

The upload is one object per row, never overwriting:

```bash
python -c "import json,pathlib;d=pathlib.Path('rr');d.mkdir(exist_ok=True);[(d/(r['date']+'.json')).write_text(json.dumps(r)+'
') for r in map(json.loads,open('operations/ledger/backfill/race-report.jsonl'))]"
gcloud storage cp --no-clobber rr/*.json gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/race-report/
```

The dates are all before the Routine's first write, so none collides with an
object the Routine will create.

### Deriving the scoreboard

```bash
python operations/ledger/derive_scoreboard.py operations/ledger/backfill/race-report.jsonl --out docs/scoreboard.json
```

It validates every row (the rollup against `requests`, `booked` against
`outcome`, `member` null, one row per date) before deriving anything. It derives
the outcome split only. The streak is published as unavailable while every row is
a backfill row. The first Routine-written row makes the script exit rather than
publish a streak it has no walk for; that walk belongs to the scoreboard Routine
(#234).
