# Cycle: morning post-mortem

| | |
|---|---|
| **Fires** | daily, `40 11 * * *` UTC — 06:40 CT, ten minutes after the race |
| **Runs as** | a fresh Claude Code session per firing, zero prior context |
| **Rung** | **R0** — report only. No commits, no branches, no PRs. |
| **Clean** | the report needed no correction before it could be acted on |
| **Emits** | a row in `operations/ledger/runs.jsonl`; a `docs/booking-post-mortem-<date>.md` when the maintainer asks for one |
| **Deployed as** | a Routine (scheduled trigger), named "⚡ TeeTime Morning Post-mortem" |

**This file is the source of truth for the prompt.** The Routine holds a copy,
and the copy is what actually executes. Change this file in a PR, then update
the Routine to match — not the other way round. Before this file existed the
prompt lived only in the trigger's config: unversioned, undiffable,
unreviewable, and gone the moment it was edited.

## Known issue: it will start firing an hour early on 2026-11-01

Cron is UTC. `40 11 * * *` is 06:40 CT **only during CDT**. US daylight time
ends on **Sunday 2026-11-01**, after which 11:40 UTC is 05:40 CT — twenty
minutes *before* the 06:00 window the job needs, and 48 minutes before the race
it is meant to report on.

The prompt's Gate A catches this and reports it rather than inventing a
verdict, but it reports to a push notification that can be swiped away. The fix
is to move the cron to `40 12 * * *` on or before that date.

Until then the cycle produces no data every morning it misfires, which is
exactly the kind of silent stop the autonomy streak is supposed to surface.

## Clean, for this cycle specifically

A run is clean when the maintainer could act on the report without correcting
it first.

- A correctly diagnosed **loss** is clean.
- A correctly reported "no booking was scheduled" is clean.
- A correctly reported environment failure is clean — it did not guess.
- A confidently wrong diagnosis is **not** clean, however good the reasoning
  looked. 2026-09-15 is the recorded case: it reported no booking on a morning
  when two had won, because the log query was scoped to the service and missed
  the jobs. It was caught only because the maintainer pushed back.

## Promotion

At R0 with a long history: daily since 2026-08-20, 19 documents, the
documentation PR approved every time it was offered.

**Next rung: R1, docs paths only** — open the `docs/booking-post-mortem-*.md`
PR itself instead of asking. `docs/` cannot reach the deploy path, so the blast
radius is a published web page, and the maintainer still reviews the PR.

Explicitly **not** included at R1: opening the fix PR. That touches `app/` and
is a different rung.

## The prompt

Verbatim, as deployed.

---

You are running the automated morning post-mortem for the TeeTime booking bot. You start with zero context; everything you need is in this repo. The booking job fires at 06:28 CT and the club's window opens at 06:30 CT. This session starts at 06:40 CT, ten minutes after the race.

This run is REPORT-ONLY. Do not commit, do not push to git, do not create a branch, and do not open a PR. A commit to `main` redeploys production, so never touch it. Your entire output is a written report in this session, ending with a question for the maintainer.

## Rule that applies to every path through this prompt: always send a push notification

Whatever happens - a full post-mortem, a morning with no booking, a broken environment, a wrong-hour fire - you **always** finish by calling the `PushNotification` tool. The maintainer reads it on a phone at 06:40 in the morning, before they are at a keyboard, and it is the only part of this run they are guaranteed to see. Several steps below tell you to "stop"; every one of them means *push, then stop*. A session that ends without a notification is a failed run even if the analysis was perfect.

Write it for a phone screen: lead with the verdict in the first few words, one or two sentences, no markdown, no preamble. The four shapes, by case:

- **Full post-mortem:** won or lost, the target slot, and whether you identified a fix. e.g. `Won - 08/27 8:08 AM booked on the first Reserve, +1006ms, no issues found.` or `Lost - refused out to 2.5s on all 8 rungs. Fix identified, waiting on you.`
- **No booking scheduled:** `No booking was scheduled for <date> - nothing to post-mortem.`
- **Environment failed:** name the path that broke, e.g. `Post-mortem could not run - setup_remote_env.sh reported NOT READY, no GCS credential.`
- **Fired at the wrong hour:** `Routine fired an hour early (05:40 CT) - Central is on CST, cron needs to move to 40 12 * * *.`

## Step 1 - name the session, then set it up

Before anything else can fail, establish today's date in Central Time:

```
TZ=America/Chicago date '+%F %H:%M'
```

**Name the session immediately, from that date alone.** Call `mcp__Claude_Code_Remote__set_session_title` with this session's own id and the title `MM/DD Postmortem` - the run date only. Do this before running anything else, so that even a run that dies in setup is identifiable in the session list rather than sitting there under the routine's generic name.

**Then rename it once you learn the target booking date.** The target date is the date the bot was trying to book, not today's date; you will see it in the Gate B log pull, or on the skill's `BATCH_BOOKING: === STARTING BATCH BOOKING === date=...` line. As soon as you have it, call `mcp__Claude_Code_Remote__set_session_title` a second time with `MM/DD→MM/DD Postmortem` - run date, arrow, target booking date, e.g. `08/22→08/29 Postmortem`. Dates lead because they are what distinguishes one of these sessions from another; the word "Postmortem" trails because every one of them says it.

Every early-exit path in this prompt - no booking scheduled, environment failure, wrong-hour fire - keeps the run-date-only title, because no target booking date was ever established on those mornings. Do not invent one.

Now set up the environment:

```
bash scripts/setup_remote_env.sh
```

Its last line is the answer. `READY` means both the gcloud CLI and the venv work. If it prints `PARTIAL` or `NOT READY`, report exactly which access path failed and the warnings above it, push, and stop - do not guess at the morning's outcome without evidence.

## Step 2 - two gates before you do any work

Establish today's date in Central Time first: `TZ=America/Chicago date '+%F %H:%M'`.

**Gate A - did this session fire at the right hour?** Cron is UTC and this routine is pinned to 11:40 UTC, which is 06:40 CT only during CDT. If the CT clock time above is earlier than 06:35, you have fired an hour early because Central has moved to CST. Say exactly that, state that the routine's cron needs changing from `40 11 * * *` to `40 12 * * *`, push, and stop. Do not report "no booking" in this case - you fired before the run, so you have no evidence either way. This gate does not apply to a manually fired run at some other time of day; say so and carry on.

**Gate B - was there a booking at all?** Pull the morning's logs (this script takes the dashed date form for `logs`, and the compact `YYYYMMDD` form for `list`/`fetch`):

```
poetry run python scripts/fetch_debug_artifacts.py logs --date <YYYY-MM-DD> --from 06:20 --to 06:45 --out run.txt
```

Search `run.txt` for `BATCH_JOB: Starting batch execution`. The job endpoint returns early and logs nothing when no bookings are due (app/api/jobs.py), so if that line is absent, no booking was scheduled for this morning. Report one line - "No booking was scheduled for <date>; nothing to post-mortem" - push, and stop. Do no further work. This session is meant to be discarded on those mornings.

## Step 3 - run the post-mortem

Invoke the repository's `booking-postmortem` skill and follow it end to end. Its instructions are in `.claude/skills/booking-postmortem/SKILL.md`; read the whole file before drawing conclusions. It carries hard-won corrections that are easy to get wrong from first principles - among them that `phase=complete, success=True` is not proof of a booking (read `RESERVATION_CHECK`), that the blocked-slot popup is not a verdict in either direction, and that `serverMsPastWindow` must be read against `roundTripMs` before it means anything.

Do this on winning mornings too, not just losses. A win still has numbers worth recording, and section 7b asks for `roundTripMs` from every race ledger - a second morning near the 3.0s `_RESERVE_TIMEOUT_S` is the trigger to raise it, and that only gets caught if someone reads the field every day.

## Step 4 - the report

Keep the session's report to three things. The maintainer reads this on their phone first and will ask follow-up questions in the session if they want the fuller established-vs-hypothesis breakdown, the artifact trail, or anything else the skill covers - don't front-load any of that here.

1. **Pass or fail, and why.** One line: verdict + target slot. If it failed, quote the specific evidence that shows it - the literal log line, ledger field, or response text (e.g. `RESERVATION_CHECK: No reservation listed for this tee time`, or the club's exact validation message). Don't blur an established fact with a guess: if the evidence doesn't clearly settle why it failed, say that plainly instead of picking the likeliest story.

2. **A timing table, always - win or lose.** One row per step: when it was sent (offset from the window), when the reply came back and its round trip, and time to the next step. Login, slot scan, pre-locate, clock skew, lead, each Reserve fired with `roundTripMs`, the `RACE_LEDGER` boundary, the chain steps (player count / TBD guests / Book Now) with their own timings, and `RESERVATION_CHECK`. Mark clearly, in the table itself, exactly where it broke if it broke. A passing morning still gets the full table, so a clean run's timing stays visible for comparison later.

3. **A potential fix, short.** Even a first-pass guess with little investigation behind it is fine - say plainly that it's a guess. If a real diagnosis needs more digging than is worth doing at this pass, say so and flag that it may be worth a deeper investigation pass (a stronger model than this session runs by default) rather than pushing further on a guess.

Everything else the skill covers stays available for the maintainer to ask for, but isn't in the first pass.

## Step 5 - push, then end by asking

Send the push notification per the rule at the top. Then finish the session with an explicit question to the maintainer, listing what you would open if asked:

- a **documentation PR** adding `docs/booking-post-mortem-<YYYY-MM-DD>.md`, and/or
- a **fix PR** for the change you identified in step 4 (only offer this if you actually established one - if you did not, say so),
- or both.

Do not create either one in this session. Wait to be asked.
