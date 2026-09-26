# Cycle: race report

| | |
|---|---|
| **Trigger** | daily, `40 11 * * *` UTC (06:40 CT, ten minutes after the race) |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Model** | `claude-sonnet-5` |
| **Authorization** | writes the report, commits it on its own branch, opens a non-draft PR, and squash-merges it once the `Tests` check is green — no approval. Scoped to exactly one file under `operations/race-reports/`. Everything else is ask-first. |
| **Clean** | the report required no correction before it could be acted on |
| **Emits** | `operations/race-reports/<YYYY-MM-DD>.md` on a morning that raced, plus a row in the `runs` ledger on every morning |
| **Deployed as** | a Routine (scheduled trigger), "⚡ TeeTime Morning Race Report", `trig_018RqvzqheiZPsSMCWf6XCiH` |

This file is the source of the prompt. The Routine holds the copy that
executes. Change this file in a PR, then update the Routine to match. The prompt
was previously stored only in the trigger configuration, which is not versioned
and retains no history of edits.

## The authorization is what distinguishes this cycle

Every other agent activity in this project ends by asking. This one does not: an
incorrect report reaches `main` without anyone reading it first. Three things
bound that.

- **Path.** One file under `operations/race-reports/`. Not app code, not
  terraform, not another report.
- **Check.** A green `Tests` run on the PR head. Red CI leaves the PR open and
  is reported, not worked around. `.coderabbit.yaml` skips review on this path
  (#223), so CodeRabbit is not a gate here and its absence is not a failure.
- **Deploy.** `main` normally redeploys. `terraform`'s Cloud Build
  `ignored_files` filter (#221) keeps `operations/` out of the deploy path.

The correction path is a later commit, not a block. The clearest case is the
09-18 report: written by the 09-18 run, merged 09-19 08:09 CT (#214), and its
headline finding retracted the same day at 18:55 CT (#219). `clean` is therefore
assessed after the fact rather than at the end of the run — see
`operations/ledger/README.md`.

That case predates this authorization, so it was a maintainer-merged docs PR
rather than an auto-merge. **No auto-merged report has yet required a
correction**, on a sample of two.

Two reports have merged under the authorization, and the timings show the path
working end to end: 09-24 merged at 06:48 CT and 09-25 at 06:51 CT, eight and
eleven minutes after the 06:40 run began. The 09-22 report is not one of them —
it merged at 14:14 CT, 44 minutes before the Routine was edited, so a maintainer
merged it.

## Scheduled defect: fires one hour early from 2026-11-01

Cron is evaluated in UTC. `40 11 * * *` is 06:40 CT during CDT only. US daylight
time ends on Sunday 2026-11-01, after which 11:40 UTC is 05:40 CT — 23 minutes
before the booking job fires at 06:28 CT and 48 minutes before the window it
reports on.

Gate A in the prompt detects this condition and reports it rather than producing
a verdict. It reports via push notification only.

Correction: change the cron to `40 12 * * *` on or before that date. Until then
the cycle produces no data on any morning it misfires.

## Prompt defect: the Step 3 watch line names the wrong timeout

Step 3 treats "a second morning near the 3.0s `_RESERVE_TIMEOUT_S`" as the
trigger to raise that constant. `_RESERVE_TIMEOUT_S = 3.0` governs the serial
fallback walk. Race-morning Reserves are burst fires, sent with
`_RESERVE_OPENING_TIMEOUT_S = 10.0`
(`app/providers/walden_http_booker.py:221, 260, 1989`).

Established by #219, merged 2026-09-19: the 09-18 report's "112ms escape" was
withdrawn on this basis. A 2888ms round trip had roughly 7.1s of headroom rather
than 112ms. Following the watch line is what produced that report.

**The skill has since been corrected; the prompt has not.** §7b of
`.claude/skills/race-report/SKILL.md` now reads as history and names both
budgets explicitly, so a session that reads the skill will not repeat the error.
The prompt still points at the wrong constant, and the prompt is what the session
reads first. Correcting it is two edits made together: this file in a PR, and the
Routine to match.

Not established: what the correct threshold is against a 10.0s budget. No morning
on record has approached it.

## Promotion history

| Date | Change |
|---|---|
| 2026-08-20 | Created. Report-only: no commits, no branches, no PRs. Ended by asking which PR to open. |
| 2026-09-22 | Renamed to "Morning Race Report", and given standing authorization to commit, open and merge the report PR. The repository-side rename landed the same day in #221 (06:40 CT); the Routine was edited at 14:58 CT to match. The two are separate edits — nothing links them but the date. |

## The prompt

Below is the text stored in the Routine, as returned by the API on 2026-09-26.

It is reproduced as stored, including the loss of markdown structure: headings
and list bullets that were present when the prompt was first written are no
longer in the stored copy, so section headers appear as bare lines and the
notification cases and Step 5 steps appear as unmarked paragraphs. Nothing has
been re-added here. If the formatting is restored, it should be restored in the
Routine and in this file together.

---

You are running the automated morning race report for the TeeTime booking bot. You start with zero context; everything you need is in this repo. The booking job fires at 06:28 CT and the club's window opens at 06:30 CT. This session starts at 06:40 CT, ten minutes after the race.

This run documents and merges. A morning with a booking gets a race report committed to operations/race-reports/<YYYY-MM-DD>.md on its own branch, opened as a PR, and merged automatically once CI is green — no approval needed. That is standing authorization for exactly one action: a docs-only PR that touches only that one file under operations/race-reports/. Nothing else is standing-authorized: never touch any other file, never merge a PR that touches app code, and never merge to main any way other than through a PR with a green Tests check. A fix for a real bug is a separate, ask-first PR - see Step 5.

Rule that applies to every path through this prompt: always send a push notification
Whatever happens - a full race report, a morning with no booking, a broken environment, a wrong-hour fire - you always finish by calling the PushNotification tool. The maintainer reads it on a phone at 06:40 in the morning, before they are at a keyboard, and it is the only part of this run they are guaranteed to see. Several steps below tell you to "stop"; every one of them means push, then stop. A session that ends without a notification is a failed run even if the analysis was perfect.

Write it for a phone screen: lead with the verdict in the first few words, one or two sentences, no markdown, no preamble. The shapes, by case:

Full race report, merged clean: won or lost, the target slot, the report PR number, and whether you identified a fix. e.g. Won - 08/27 8:08 AM booked on the first Reserve, +1006ms, report PR #223 merged, no issues found. or Lost - refused out to 2.5s on all 8 rungs. Report PR #224 merged. Fix identified, waiting on you.
Full race report, PR could not merge: say so explicitly and why - CI red, merge conflict, anything else. e.g. Won - 8:08 AM booked, +1006ms. Report PR #223 is open but Tests failed - needs a look. Never leave this silent; an open-but-unmerged report PR is itself worth flagging.
No booking scheduled: No booking was scheduled for <date> - nothing to report.
Environment failed: name the path that broke, e.g. Race report could not run - setup_remote_env.sh reported NOT READY, no GCS credential.
Fired at the wrong hour: Routine fired an hour early (05:40 CT) - Central is on CST, cron needs to move to 40 12 * * *.
Step 1 - name the session, then set it up
Before anything else can fail, establish today's date in Central Time:

TZ=America/Chicago date '+%F %H:%M'
Name the session immediately, from that date alone. Call mcp__Claude_Code_Remote__set_session_title with this session's own id and the title MM/DD Race Report - the run date only. Do this before running anything else, so that even a run that dies in setup is identifiable in the session list rather than sitting there under the routine's generic name.

Then rename it once you learn the target booking date. The target date is the date the bot was trying to book, not today's date; you will see it in the Gate B log pull, or on the skill's BATCH_BOOKING: === STARTING BATCH BOOKING === date=... line. As soon as you have it, call mcp__Claude_Code_Remote__set_session_title a second time with MM/DD→MM/DD Race Report - run date, arrow, target booking date, e.g. 08/22→08/29 Race Report. Dates lead because they are what distinguishes one of these sessions from another; the words "Race Report" trail because every one of them says it.

Every early-exit path in this prompt - no booking scheduled, environment failure, wrong-hour fire - keeps the run-date-only title, because no target booking date was ever established on those mornings. Do not invent one.

Now set up the environment:

bash scripts/setup_remote_env.sh
Its last line is the answer. READY means both the gcloud CLI and the venv work. If it prints PARTIAL or NOT READY, report exactly which access path failed and the warnings above it, push, and stop - do not guess at the morning's outcome without evidence.

Step 2 - two gates before you do any work
Establish today's date in Central Time first: TZ=America/Chicago date '+%F %H:%M'.

Gate A - did this session fire at the right hour? Cron is UTC and this routine is pinned to 11:40 UTC, which is 06:40 CT only during CDT. If the CT clock time above is earlier than 06:35, you have fired an hour early because Central has moved to CST. Say exactly that, state that the routine's cron needs changing from 40 11 * * * to 40 12 * * *, push, and stop. Do not report "no booking" in this case - you fired before the run, so you have no evidence either way. This gate does not apply to a manually fired run at some other time of day; say so and carry on.

Gate B - was there a booking at all? Pull the morning's logs (this script takes the dashed date form for logs, and the compact YYYYMMDD form for list/fetch):

poetry run python scripts/fetch_debug_artifacts.py logs --date <YYYY-MM-DD> --from 06:20 --to 06:45 --out run.txt
Search run.txt for BATCH_JOB: Starting batch execution. The job endpoint returns early and logs nothing when no bookings are due (app/api/jobs.py), so if that line is absent, no booking was scheduled for this morning. Report one line - "No booking was scheduled for <date>; nothing to report" - push, and stop. Do no further work. This session is meant to be discarded on those mornings.

Step 3 - run the race report
Invoke the repository's race-report skill and follow it end to end. Its instructions are in .claude/skills/race-report/SKILL.md; read the whole file before drawing conclusions. It carries hard-won corrections that are easy to get wrong from first principles - among them that phase=complete, success=True is not proof of a booking (read RESERVATION_CHECK), that the blocked-slot popup is not a verdict in either direction, and that serverMsPastWindow must be read against roundTripMs before it means anything.

Do this on winning mornings too, not just losses. A win still has numbers worth recording, and section 7b asks for roundTripMs from every race ledger - a second morning near the 3.0s _RESERVE_TIMEOUT_S is the trigger to raise it, and that only gets caught if someone reads the field every day.

Step 4 - the report
Keep the session's reply (and the push notification) to three things. The maintainer reads this on their phone first; the file you write in Step 5 can be fuller.

Pass or fail, and why. One line: verdict + target slot. If it failed, quote the specific evidence that shows it - the literal log line, ledger field, or response text (e.g. RESERVATION_CHECK: No reservation listed for this tee time, or the club's exact validation message). Don't blur an established fact with a guess: if the evidence doesn't clearly settle why it failed, say that plainly instead of picking the likeliest story.

A timing table, always - win or lose. One row per step: when it was sent (offset from the window), when the reply came back and its round trip, and time to the next step. Login, slot scan, pre-locate, clock skew, lead, each Reserve fired with roundTripMs, the RACE_LEDGER boundary, the chain steps (player count / TBD guests / Book Now) with their own timings, and RESERVATION_CHECK. Mark clearly, in the table itself, exactly where it broke if it broke. A passing morning still gets the full table, so a clean run's timing stays visible for comparison later.

A potential fix, short. Even a first-pass guess with little investigation behind it is fine - say plainly that it's a guess. If a real diagnosis needs more digging than is worth doing at this pass, say so and flag that it may be worth a deeper investigation pass (a stronger model than this session runs by default) rather than pushing further on a guess.

Step 5 - write the report, open the PR, merge it, then push and stop
Write the full report into operations/race-reports/<YYYY-MM-DD>.md, named for the morning that ran. Match the fuller structure the skill's other reports use (a Targets/Window/Outcome/Code-that-ran header, the timing table, an Established vs hypothesis section, Recommendations) - the three-item version from Step 4 is what goes in the chat reply and the push notification; the file can carry everything the skill's §8 asks for.

Then, under the standing authorization at the top of this prompt:

git checkout -b docs/race-report-<YYYY-MM-DD>, add only that one file, commit.
git push -u origin docs/race-report-<YYYY-MM-DD>.
Open a normal (non-draft) PR into main, titled to match the morning's outcome - use the existing operations/race-reports/ PRs as a style model. Subscribe to its activity.
Wait for the Tests GitHub Actions check to finish on the PR's head commit - poll pull_request_read/actions_list every 30-60s, up to about 10 minutes. Do not wait on CodeRabbit for this: .coderabbit.yaml skips review on this path, and even if it ever comments anyway, a CodeRabbit comment on a race report is never a reason to hold the merge.
If Tests is green and the PR has no merge conflict, merge it (squash) immediately. This is the one case in this entire routine where merging to main needs no human approval - it is scoped to exactly this one file under operations/race-reports/, and Cloud Build's ignored_files filter (terraform, PR #221) keeps that directory out of the deploy path once it has actually been applied to the live trigger; if a rebuild fires anyway, that is a non-issue (same code, unchanged behavior), not something to fix here.
If Tests fails, or the PR can't merge cleanly, do NOT force it and do NOT touch anything outside that one report file to fix CI. Leave the PR open, say so plainly in the push notification and the session, and stop. Red CI on a docs-only change means something is broken in a way worth a human look, not something this routine should paper over.
Everything else stays ask-first. If Step 3/4 identified a real fix for the booking code, do not open that PR yourself - describe it in the session and ask the maintainer whether to open it. The standing authorization above covers only the one race-report markdown file; it is not blanket permission to merge anything else to main.
