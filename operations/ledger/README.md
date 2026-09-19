# The ledger

Cycles append rows here. The scoreboard is a rollup over these files and never
re-parses prose.

**Why rows and not documents.** The post-mortems in `docs/` are the reasoning —
what was established, what was only hypothesis, what the evidence could not
settle. That is what they are good at and they should stay that way. But
computing a win rate by re-reading nineteen markdown files is slow, and worse,
it is non-deterministic: two sessions can read the same document and score it
differently. The row is the fact; the document is the argument.

One row per event, appended, never rewritten. JSON Lines so a row can be added
without reading or rewriting the file.

---

## `runs.jsonl` — one row per cycle run

Feeds the autonomy streak. Every cycle appends here, on every run, including
runs that did nothing.

```json
{"cycle":"morning-postmortem","date":"2026-09-15","rung":0,"clean":false,
 "note":"reported no booking; two had won. stale service_name query, fixed in #206"}
```

| Field | Meaning |
|---|---|
| `cycle` | matches a file in `operations/cycles/` |
| `date` | run date, CT |
| `rung` | the rung it was operating at, 0–5 |
| `clean` | did the output need correction before it could be used — **not** whether the news was good |
| `note` | required when `clean` is false; what went wrong |

The streak is `clean: true` rows counted back from newest until the first
`false`. Never store the streak itself.

## `mornings.jsonl` — one row per booking request

Feeds the outcome split.

```json
{"date":"2026-09-15","target_date":"2026-09-22","requester":"<id>",
 "requested":"08:10","booked":"08:10","outcome":"exact",
 "confirmed_slot":true,"source":"RESERVATION_CHECK"}
```

`outcome` is one of `exact` / `fallback` / `miss`, decided by
`RESERVATION_CHECK` alone.

`confirmed_slot` records whether the requested time had been resolved to a real
bookable slot and agreed with the member before the race (#216). **It is
`false` for every row predating that feature**, and `exact` on such a row means
only "got the time asked for", which may or may not have been a slot that
existed. Do not compare the two eras without saying so.

## `changes.jsonl` — one row per merged PR

Feeds fix latency.

```json
{"pr":206,"identified":"2026-09-15","merged":"2026-09-16",
 "source":"docs/booking-post-mortem-2026-09-15.md","human_edited":false}
```

`identified` is when a post-mortem or review named the fix — not when the bug
was introduced, and not when it was first observed.

`human_edited` is there for human-touch rate if that metric is ever adopted;
record it either way, since it cannot be reconstructed later.

---

## Backfill

**These files start empty.** Nineteen mornings of history exist in `docs/` and
are not represented here.

They are deliberately not backfilled by guesswork. Two obstacles, both real:

- `outcome` under the current definition depends on `confirmed_slot`, which was
  false for all of them — so every historical row would need the caveat above
  to mean anything.
- `clean` for a past post-mortem run is a judgement about whether its output
  needed correcting, which the documents do not record directly. 2026-09-15 is
  knowable because the correction is written down. Most are not.

Backfill from the documents when there is a reason to, one at a time, marking
anything inferred rather than read. An invented row is worse than a missing
one: a gap is visible, a fabrication is not.
