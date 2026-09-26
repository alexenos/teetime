# Routine: scoreboard snapshot

**Status: proposed. Not deployed.** No trigger exists for it. This file is the
specification; deploying it is the steps in the last section.

| | |
|---|---|
| **Trigger** | daily, `30 12 * * *` UTC (07:30 CT), proposed |
| **Runs as** | a new session per firing, with no prior context |
| **Authorization** | appends to GCS and sends a notification. **Commits nothing, opens no PR, merges nothing.** |
| **Emits** | one row in `operations/ledger/snapshots.jsonl` |
| **Clean** | the row was appended and its values were read from the stated sources, not inferred |

## Why this is separate from the race report

It would be cheaper to append the snapshot at the end of the race report
Routine, which already runs daily and already has the morning's outcome in hand.
Three things argue against it.

**The race report's failure paths all stop early.** Wrong hour, no booking
scheduled, environment not ready — every one is *push, then stop*. A snapshot at
the end of that prompt is skipped on exactly the mornings something went wrong,
which is where a gap in the history is least acceptable.

**Cost is monthly and comes from somewhere else.** It is not a fact about this
morning's race, and reading it does not belong in a session diagnosing one.

**The snapshot wants today's report already merged.** Reports have merged at
06:48 and 06:51 CT, eight and eleven minutes after their run began. 07:30 CT is
clear of that with margin, and nothing about a snapshot is time-critical — the
morning's push notification already went out an hour earlier.

## Why it commits nothing

The race report's standing authorization to merge to `main` is the one
genuinely load-bearing risk in this setup. It is bounded by a path, a check and a
deploy filter, and those bounds are worth keeping scarce. A second standing merge
authorization, for a metrics job, buys a committed file that a script can render
on demand instead.

A GCS append plus a notification satisfies both requirements without any new
write access: the append is the history, and the notification is the answer to
"did it change".

## What a run does

1. Read the three metrics per `operations/scoreboard.md`, each from the source
   that document names. Record `null` with a reason for any source that does not
   exist — do not infer a value, and do not substitute zero.
2. Append one row to `snapshots.jsonl`, carrying the `definitions` version.
3. Read the previous row. Compare.
4. **Notify only if something moved.** A day where every value is unchanged
   needs no notification; the row is the record that the check happened. A day
   where a value changed, or where a source that previously worked has stopped
   working, does.
5. If the append itself fails, notify. A silent gap in the history is the one
   failure mode this Routine exists to prevent.

## Shares the DST defect

`30 12 * * *` UTC is 07:30 CT during CDT only. US daylight time ends
2026-11-01, after which it fires at 06:30 CT — during the race, and before the
report it depends on has merged. The correction is `30 13 * * *`, and it is the
same correction the race report needs on the same date. Deploying this Routine
adds a second cron to change, so change both together.

## Deploying it

1. Create the Routine with the prompt built from the steps above, fresh session
   per firing.
2. Record the trigger ID in the table at the top of this file, change **Status**
   to deployed, and note the date.
3. Add its row to the automation streak: the metric currently covers one
   Routine, and `operations/scoreboard.md` says so in those words.

Until all three are done this file describes something that does not run, and
the scoreboard has no history.
