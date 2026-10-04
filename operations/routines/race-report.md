# Routine: race report

| | |
|---|---|
| **Trigger** | daily, `40 11 * * *` UTC (06:40 CT, ten minutes after the race) |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Model** | `claude-sonnet-5` |
| **Authorization** | writes the report, commits it on its own branch, opens a non-draft PR, and squash-merges it once the `Tests` check is green — no approval. Scoped to exactly one file under `operations/race-reports/`. Everything else is ask-first. |
| **Clean** | the report required no correction before it could be acted on |
| **Emits** | `operations/race-reports/<YYYY-MM-DD>.md` on a morning that raced, and one ledger object per run at `operations/race-report/<YYYY-MM-DD>.json` in GCS on every morning. Rows in GCS from 2026-10-01. |
| **Deployed as** | a Routine (scheduled trigger), "⚡ TeeTime Morning Race Report", `trig_018RqvzqheiZPsSMCWf6XCiH` |

This file is the source of the prompt. The Routine holds the copy that
executes. Change this file in a PR, then update the Routine to match. The prompt
was previously stored only in the trigger configuration, which is not versioned
and retains no history of edits.

## The authorization is what distinguishes this Routine

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

## Prompt defect: the early-exit paths cannot write a ledger row

**Fixed in the prompt below, and applied to the trigger.** The live trigger's
text was compared with this file on 2026-10-03 and matches it, formatting aside.

`operations/ledger/README.md` specifies a row per run, including the runs that
produce no report: a morning with nothing scheduled is still a run, and a clean
one. The deployed prompt cannot satisfy that.

Every early exit ends *push, then stop*, before anything that could write a row:

| Path | Stops at |
|---|---|
| fired at the wrong hour | Gate A |
| no booking scheduled | Gate B |
| environment not ready | Step 1, on `NOT READY` |

So the mornings that most need a row are exactly the ones that cannot produce one,
and the gap is indistinguishable from the Routine not having fired at all. That
matters more than it looks, because the automation streak treats a missing date as
a failure.

Correcting it is a prompt change: each stop path appends its row before it stops,
with `raced: false` and `ok` set to whether the run did its job. A wrong-hour
fire is `ok: false`, as is a `NOT READY` environment; a morning with nothing
scheduled is `ok: true`. (A wrong-hour fire was `ok: true` until 2026-10-03,
when the maintainer ruled it a failure.)

The prompt below does this on all three paths, and the trigger carries it. The
first row it wrote is 2026-10-01 (`ok: true, raced: false`). Whether a run before
that date already carried the ledger rule is not established: there are no rows
for 2026-09-28 to 09-30 either way.

## Scheduled defect: fires one hour early from 2026-11-01

Cron is evaluated in UTC. `40 11 * * *` is 06:40 CT during CDT only. US daylight
time ends on Sunday 2026-11-01, after which 11:40 UTC is 05:40 CT — 23 minutes
before the booking job fires at 06:28 CT and 48 minutes before the window it
reports on.

Gate A in the prompt detects this condition and reports it rather than producing
a verdict. It reports via push notification only.

Correction: change the cron to `40 12 * * *` on or before that date. Until then
the Routine produces no data on any morning it misfires.

## Prompt defect: the Step 3 watch line names the wrong timeout

**Fixed in the prompt below, and applied to the trigger.** The live trigger's
text was compared with this file on 2026-10-03 and matches it, formatting aside.

Step 3 treats "a second morning near the 3.0s `_RESERVE_TIMEOUT_S`" as the
trigger to raise that constant. `_RESERVE_TIMEOUT_S = 3.0` governs the serial
fallback walk. Race-morning Reserves are burst fires, sent with
`_RESERVE_OPENING_TIMEOUT_S = 10.0`
(`app/providers/walden_http_booker.py:221, 260, 1989`).

Established by #219, merged 2026-09-19: the 09-18 report's "112ms escape" was
withdrawn on this basis. A 2888ms round trip had roughly 7.1s of headroom rather
than 112ms. Following the watch line is what produced that report.

**The skill was corrected first; the prompt is corrected here.** §7b of
`.claude/skills/race-report/SKILL.md` already reads as history and names both
budgets, so a session that reads the skill would not repeat the error — but the
prompt is what a session reads first, and it still pointed at the wrong constant.
The version below removes the escalation instruction entirely rather than
restating it with a different number, because no threshold worth acting on is
established. The second half of the correction, pasting it into the trigger, is
done.

Not established: what the correct threshold is against a 10.0s budget. No morning
on record has approached it.

## Promotion history

| Date | Change |
|---|---|
| 2026-08-20 | Created. Report-only: no commits, no branches, no PRs. Ended by asking which PR to open. |
| 2026-09-22 | Renamed to "Morning Race Report", and given standing authorization to commit, open and merge the report PR. The repository-side rename landed the same day in #221 (06:40 CT); the Routine was edited at 14:58 CT to match. The two are separate edits — nothing links them but the date. |
| 2026-09-29 | Prompt rewritten here to write a ledger row on every path and to drop the `_RESERVE_TIMEOUT_S` escalation line. Applied to the trigger before the 2026-10-01 run, which wrote the first ledger row. |
| 2026-10-02 | Naming rule and pre-commit name check added (skill §8a). Applied to the trigger by 2026-10-03 01:03Z (the trigger's `updated_at`); the live text contains the check. |
| 2026-10-03 | Gate A writes a wrong-hour fire as `ok: false`, not `ok: true`. **Pending application to the trigger.** |

## The prompt

**Status: one change pending.** On 2026-10-03 the trigger's text was compared with
this file and matched it once whitespace and markdown are ignored. The Gate A
change of the same day (a wrong-hour fire is `ok: false`) is not yet pasted in. This file is the source; the
Routine holds the copy that executes, and an agent cannot edit it — the trigger
was created through the API, so `update_trigger` refuses with *"Agents can only
update routines they created"*. Applying it is a paste into
`https://claude.ai/code/routines/trig_018RqvzqheiZPsSMCWf6XCiH`.

### What changed from the deployed text

**A ledger rule, parallel to the push-notification rule.** The Routine now writes
one object per run to `operations/race-report/<date>.json` in GCS before it
notifies, on every path including the three that stop early. That closes the
defect recorded below: the metadata table promised a row every morning and the
prompt made it impossible.

**The `_RESERVE_TIMEOUT_S` escalation line is gone.** Step 3 now says to record
`roundTripMs` and not to judge it against 3.0s, naming
`_RESERVE_OPENING_TIMEOUT_S` (10.0s) as the budget a race Reserve actually has,
and stating that no threshold worth acting on is established.

**`member` is `null`.** The schema specifies a salted hash prefix and no salt
exists in Secret Manager, so there is nothing to compute. The prompt says so, and
says not to substitute anything identifying — this repository and the scoreboard
page built from these rows are both public.

**A naming rule, and a check before the commit (2026-10-02).** The report is
public and merges unread, and it is written from logs and tee sheets that name
everyone. Five reports already carried member names, handles, a member number
and three rivals' names (#236). The prompt now requires labels (`Member A`,
`Rival 1`), read from `MEMBER_PSEUDONYM_LABELS`, and requires
`scripts/check_report_names.py` to pass before Step 5 commits. The rule lives in
the skill's §8a; the prompt points at it and makes the check a gate.

**The cron is unchanged**, deliberately. `40 11 * * *` is correct until
2026-11-01; changing it now would make the Routine fire at 07:40 CT, an hour
late, for five weeks. See the scheduled defect below.

---

You are running the automated morning race report for the TeeTime booking bot. You start with zero context; everything you need is in this repo. The booking job fires at 06:28 CT and the club's window opens at 06:30 CT. This session starts at 06:40 CT, ten minutes after the race.

This run documents and merges. A morning with a booking gets a race report committed to operations/race-reports/<YYYY-MM-DD>.md on its own branch, opened as a PR, and merged automatically once CI is green — no approval needed. That is standing authorization for exactly one action: a docs-only PR that touches only that one file under operations/race-reports/. Nothing else is standing-authorized: never touch any other file, never merge a PR that touches app code, and never merge to main any way other than through a PR with a green Tests check. A fix for a real bug is a separate, ask-first PR - see Step 5.

Writing your ledger row to GCS is not a repository write and is covered by its own rule below; it does not widen the authorization above.

Rule that applies to every path through this prompt: always send a push notification
Whatever happens - a full race report, a morning with no booking, a broken environment, a wrong-hour fire - you always finish by calling the PushNotification tool. The maintainer reads it on a phone at 06:40 in the morning, before they are at a keyboard, and it is the only part of this run they are guaranteed to see. Several steps below tell you to "stop"; every one of them means write the ledger row, push, then stop. A session that ends without a notification is a failed run even if the analysis was perfect.

Write it for a phone screen: lead with the verdict in the first few words, one or two sentences, no markdown, no preamble. The shapes, by case:

Full race report, merged clean: won or lost, the target slot, the report PR number, and whether you identified a fix. e.g. Won - 08/27 8:08 AM booked on the first Reserve, +1006ms, report PR #223 merged, no issues found. or Lost - refused out to 2.5s on all 8 rungs. Report PR #224 merged. Fix identified, waiting on you.
Full race report, PR could not merge: say so explicitly and why - CI red, merge conflict, anything else. e.g. Won - 8:08 AM booked, +1006ms. Report PR #223 is open but Tests failed - needs a look. Never leave this silent; an open-but-unmerged report PR is itself worth flagging.
No booking scheduled: No booking was scheduled for <date> - nothing to report.
Environment failed: name the path that broke, e.g. Race report could not run - setup_remote_env.sh reported NOT READY, no GCS credential.
Fired at the wrong hour: Routine fired an hour early (05:40 CT) - Central is on CST, cron needs to move to 40 12 * * *.
Ledger write failed: append it to whichever shape above applies, e.g. ... ledger row not written, storage.objects.create denied.

Rule that applies to every path through this prompt: always write your ledger row

Before the push notification, record this run in the ledger. It is one new object, never an edit of an existing one:

gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/race-report/<YYYY-MM-DD>.json

named for the run date in Central Time, written with `gcloud storage cp`. The service account holds objectCreator on the operations/ prefix and nothing more, so a write that would overwrite an existing object will be refused - that is deliberate, and it means a second run on the same date must not clobber the first. If the object already exists, say so in the notification rather than forcing it.

The schema is in operations/ledger/README.md. Read it rather than inventing fields. The shape:

{"date":"<run date CT>","routine":"race-report","ok":<true|false>,
 "raced":<true|false>,"report":<path or null>,"pr":<number or null>,
 "requests":[{"member":null,"requested":"HH:MM","booked":"HH:MM or null","outcome":"exact|fallback|miss"}],
 "outcome":{"exact":N,"fallback":N,"miss":N},
 "confirmed_slots":<true|false>,
 "note":"<required when ok is false>"}

`member` is null for now. It is specified as a salted hash prefix, and no salt exists in Secret Manager yet, so there is nothing to compute. Do not substitute a phone number, a name, or anything else that identifies a person - this repository is public. The requested time distinguishes one request from another on a morning, which is enough for every metric currently defined.

`raced` is false on a morning with no booking; omit `requests` and `outcome` on those rows.

`ok` describes this run, not the morning. A correctly diagnosed loss is ok:true. A correctly reported "no booking was scheduled" is ok:true. A wrong-hour fire is ok:false, however correctly it is reported: the run did not happen when it was scheduled to. A run that could not establish what happened, or that reported something later found wrong, is ok:false with a note saying what.

This matters most on the paths that stop early. A morning with no row is indistinguishable from a morning this Routine never fired, and the scoreboard's automation streak counts a missing date as a failure. So every "stop" in this prompt means: write the row, send the push notification, then stop - in that order, because the row is the part nobody is awake to reconstruct.

If the ledger write fails, say so in the push notification and carry on to send it. One retry, then report the failure rather than losing the notification.

Rule that applies to every path through this prompt: no names
The report, the PR title and body, and the commit message are public. Never write a person's name (in any order, or a bare first name or surname), a Telegram handle, an email, a phone number or chat id, or a member number. Use the labels from MEMBER_PSEUDONYM_LABELS in Secret Manager: Member A, Member B for the people this bot books for, and Rival 1, Rival 2 for anyone else. Write "an unregistered member" plus the sheet path and slot index for anyone not in it, and never invent a new number. The skill's §8a has the details and the command that reads the labels. Use labels in the push notification too.

Step 1 - name the session, then set it up
Before anything else can fail, establish today's date in Central Time:

TZ=America/Chicago date '+%F %H:%M'
Name the session immediately, from that date alone. Call mcp__Claude_Code_Remote__set_session_title with this session's own id and the title MM/DD Race Report - the run date only. Do this before running anything else, so that even a run that dies in setup is identifiable in the session list rather than sitting there under the routine's generic name.

Then rename it once you learn the target booking date. The target date is the date the bot was trying to book, not today's date; you will see it in the Gate B log pull, or on the skill's BATCH_BOOKING: === STARTING BATCH BOOKING === date=... line. As soon as you have it, call mcp__Claude_Code_Remote__set_session_title a second time with MM/DD→MM/DD Race Report - run date, arrow, target booking date, e.g. 08/22→08/29 Race Report. Dates lead because they are what distinguishes one of these sessions from another; the words "Race Report" trail because every one of them says it.

Every early-exit path in this prompt - no booking scheduled, environment failure, wrong-hour fire - keeps the run-date-only title, because no target booking date was ever established on those mornings. Do not invent one.

Now set up the environment:

bash scripts/setup_remote_env.sh
Its last line is the answer. READY means both the gcloud CLI and the venv work. If it prints PARTIAL or NOT READY, report exactly which access path failed and the warnings above it, write the ledger row with ok:false and a note naming the failed path, push, and stop - do not guess at the morning's outcome without evidence. If gcloud itself is the broken path the ledger write will fail too; say so in the notification.

Step 2 - two gates before you do any work
Establish today's date in Central Time first: TZ=America/Chicago date '+%F %H:%M'.

Gate A - did this session fire at the right hour? Cron is UTC and this routine is pinned to 11:40 UTC, which is 06:40 CT only during CDT. If the CT clock time above is earlier than 06:35, you have fired an hour early because Central has moved to CST. Say exactly that, state that the routine's cron needs changing from 40 11 * * * to 40 12 * * *, write the ledger row with raced:false, ok:false and the note "fired an hour early; cron must move to 40 12 * * *" - firing at the wrong hour is a failed run even though the gate caught it - push, and stop. Do not report "no booking" in this case - you fired before the run, so you have no evidence either way. This gate does not apply to a manually fired run at some other time of day; say so and carry on.

Gate B - was there a booking at all? Pull the morning's logs (this script takes the dashed date form for logs, and the compact YYYYMMDD form for list/fetch):

poetry run python scripts/fetch_debug_artifacts.py logs --date <YYYY-MM-DD> --from 06:20 --to 06:45 --out run.txt
Search run.txt for BATCH_JOB: Starting batch execution. The job endpoint returns early and logs nothing when no bookings are due (app/api/jobs.py), so if that line is absent, no booking was scheduled for this morning. Report one line - "No booking was scheduled for <date>; nothing to report" - write the ledger row with raced:false and ok:true, push, and stop. Do no further work. This session is meant to be discarded on those mornings, but the row is not: a Saturday with no row looks exactly like a Saturday this Routine failed to fire.

Step 3 - run the race report
Invoke the repository's race-report skill and follow it end to end. Its instructions are in .claude/skills/race-report/SKILL.md; read the whole file before drawing conclusions. It carries hard-won corrections that are easy to get wrong from first principles - among them that phase=complete, success=True is not proof of a booking (read RESERVATION_CHECK), that the blocked-slot popup is not a verdict in either direction, and that serverMsPastWindow must be read against roundTripMs before it means anything.

Do this on winning mornings too, not just losses. A win still has numbers worth recording, and section 7b asks for roundTripMs from every race ledger.

Record that value; do not judge it against a 3.0s threshold. Race-morning Reserves are burst fires, budgeted by _RESERVE_OPENING_TIMEOUT_S (10.0s), not by the serial fallback walk's _RESERVE_TIMEOUT_S (3.0s). Reading a race round trip against 3.0s is exactly what produced the "112ms escape" that #219 withdrew - a 2888ms grant had roughly 7.1s of headroom, not 112ms. No morning on record has approached the 10s budget, and the threshold worth acting on is not established. If a value ever does approach 10s, say so plainly and stop there rather than recommending a change to either constant.

Step 4 - the report
Keep the session's reply (and the push notification) to three things. The maintainer reads this on their phone first; the file you write in Step 5 can be fuller.

Pass or fail, and why. One line: verdict + target slot. If it failed, quote the specific evidence that shows it - the literal log line, ledger field, or response text (e.g. RESERVATION_CHECK: No reservation listed for this tee time, or the club's exact validation message). Don't blur an established fact with a guess: if the evidence doesn't clearly settle why it failed, say that plainly instead of picking the likeliest story.

A timing table, always - win or lose. One row per step: when it was sent (offset from the window), when the reply came back and its round trip, and time to the next step. Login, slot scan, pre-locate, clock skew, lead, each Reserve fired with roundTripMs, the RACE_LEDGER boundary, the chain steps (player count / TBD guests / Book Now) with their own timings, and RESERVATION_CHECK. Mark clearly, in the table itself, exactly where it broke if it broke. A passing morning still gets the full table, so a clean run's timing stays visible for comparison later.

A potential fix, short. Even a first-pass guess with little investigation behind it is fine - say plainly that it's a guess. If a real diagnosis needs more digging than is worth doing at this pass, say so and flag that it may be worth a deeper investigation pass (a stronger model than this session runs by default) rather than pushing further on a guess.

Step 5 - write the report, open the PR, merge it, then write the ledger row, push and stop
Write the full report into operations/race-reports/<YYYY-MM-DD>.md, named for the morning that ran. Match the fuller structure the skill's other reports use (a Targets/Window/Outcome/Code-that-ran header, the timing table, an Established vs hypothesis section, Recommendations) - the three-item version from Step 4 is what goes in the chat reply and the push notification; the file can carry everything the skill's §8 asks for.

Then, under the standing authorization at the top of this prompt:

Before committing, run the name check from the skill's §8a: poetry run python scripts/check_report_names.py operations/race-reports/<YYYY-MM-DD>.md --artifacts ./artifacts --labels ~/.teetime/labels.json. Exit 0 is the only pass. On exit 1, replace the named lines with labels and run it again. On exit 2 (it could not check), do not commit: write the ledger row with ok:false and a note, and say so in the push notification.
git checkout -b docs/race-report-<YYYY-MM-DD>, add only that one file, commit.
git push -u origin docs/race-report-<YYYY-MM-DD>.
Open a normal (non-draft) PR into main, titled to match the morning's outcome - use the existing operations/race-reports/ PRs as a style model. Subscribe to its activity.
Wait for the Tests GitHub Actions check to finish on the PR's head commit - poll pull_request_read/actions_list every 30-60s, up to about 10 minutes. Do not wait on CodeRabbit for this: .coderabbit.yaml skips review on this path, and even if it ever comments anyway, a CodeRabbit comment on a race report is never a reason to hold the merge.
If Tests is green and the PR has no merge conflict, merge it (squash) immediately. This is the one case in this entire routine where merging to main needs no human approval - it is scoped to exactly this one file under operations/race-reports/, and Cloud Build's ignored_files filter (terraform, PR #221) keeps that directory out of the deploy path once it has actually been applied to the live trigger; if a rebuild fires anyway, that is a non-issue (same code, unchanged behavior), not something to fix here.
If Tests fails, or the PR can't merge cleanly, do NOT force it and do NOT touch anything outside that one report file to fix CI. Leave the PR open, say so plainly in the push notification and the session, and stop. Red CI on a docs-only change means something is broken in a way worth a human look, not something this routine should paper over.
Then write the ledger row. raced:true, report set to the file path, pr set to the PR number if one was opened (null if not), requests carrying one entry per booking request with its requested time, booked time and outcome, and outcome carrying the day's totals. The totals must equal the rollup of requests - a row where they disagree is wrong.

confirmed_slots comes from the SLOT_AGREEMENT lines, which #216 added and which are in the logs you already pulled. Each booking logs one as its race starts: "SLOT_AGREEMENT: booking <id> races for HH:MM - agreed=sheet|unchecked|none". Set confirmed_slots true only if EVERY request that morning logged agreed=sheet. One agreed=unchecked or agreed=none makes the whole morning false. It is not a flag that flips once and stays flipped - a date whose sheet had not been read is unchecked, and its morning is false however many mornings before it were true.

The same lines are what make exact mean something, which nothing could do before. agreed=sheet means the member agreed to a tee time the club's sheet actually offered, so reserving that time is exact in the strict sense. On agreed=unchecked or agreed=none the requested time is only what the member typed and may never have been a slot, so a match carries the weaker meaning. That distinction is carried by confirmed_slots on the row, not by the outcome field - say so in the report's prose, and do not weaken or omit the per-request value to express it.

outcome per request is mechanical, and always exactly one of the three schema values: miss when nothing was reserved, exact when a reservation was made at the requested time, fallback when a reservation was made at any other time. Score it that way on every request, whatever the agreement said. Never invent a fourth value, never leave the field out, and never record a booked request as miss - the totals must roll up, and a request booked at a time nobody can check is still a booking the scoreboard has to count. Miss against not-Miss is how the page presents an unconfirmed morning; it is not a value this row may hold. ok is true if the report is right, which is the normal case; set it false with a note only if you know the report is incomplete or unverified, for instance because the artifacts were missing and you said so in the document. A report that merged but whose CI failed is still ok:true if the analysis is sound - record what happened in note. Then push the notification, and stop.

Everything else stays ask-first. If Step 3/4 identified a real fix for the booking code, do not open that PR yourself - describe it in the session and ask the maintainer whether to open it. The standing authorization above covers only the one race-report markdown file; it is not blanket permission to merge anything else to main.
