# Routine: feature development

**Status: created by the maintainer 2026-10-19 (20:37 CDT), enabled; first scheduled
fire Saturday 2026-10-24 at 08:00 CT.** This file is the specification and the source
of the prompt. The prompt reads it from `main`, so it must stay there: if it is
missing the run stops and records `ok: false`. The live prompt text was compared with
the prompt below through the triggers API on 2026-10-20 and is identical. Scope
approved 2026-10-17, label-free design 2026-10-18; the prompt has not yet run.

| | |
|---|---|
| **Trigger** | weekly, Saturday 08:00 CT: `CRON_TZ=America/Chicago 0 8 * * 6`. The same day as tech debt but four hours later before 2026-11-01 and five hours later after it (tech debt's UTC cron moves from 04:00 CDT to 03:00 CST), so the two do not compete for the hourly CodeRabbit allowance, and well clear of the 06:28 CT job. The cron is in Chicago time, so it has no DST shift |
| **Deployed as** | a Routine, "TeeTime Feature Dev", `trig_01T9XfV5N4evsjd82TkKpS3f`, in the same environment as the tech-debt and scoreboard Routines (`env_01MDpUqqDJRTVhqkh1hnrxgK`), repository `alexenos/teetime`, model `claude-opus-5-5`, Auto-fix on |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Authorization** | reads the repository and GitHub. Comments on issues. Pushes one branch and opens one PR per run. Writes one ledger object to GCS. **Never merges.** |
| **Emits** | a ranked list of the open backlog in the session summary; at most one PR per run; one ledger object per run at `operations/feature-dev/<YYYY-MM-DD>.json` in GCS |
| **Owns** | the ranking, and its own run metadata. **Not** the decision to merge, and not the priorities it ranks by |

## What it is for

The backlog held 50 open issues on 2026-10-17, most of them features, fixes and
decisions rather than lint. [`tech-debt.md`](tech-debt.md) works because its signal is
checkable: a `noqa`, a stale path or a skipped test either exists or does not.
"What should be built next" is not checkable, and this repository has misdiagnosed
three times from a signal that looked well-formed and carried no information.

So the Routine does two jobs, and the maintainer's control sits in three places that
need no label:

1. **Rank.** Read every open issue, state what is known about each, and order them by
   the maintainer's priorities, with the reason.
2. **Build one.** Take the best issue it is *eligible* to build and open a PR.

The controls are: **the priorities in this file** ("Choosing what to build": what
matters, and what to leave alone), **the eligibility filter** below (it builds only well-specified work), and **the
merge**, which is always the maintainer's. The Routine never merges, exactly as tech
debt never does.

## Choosing what to build

The maintainer's prioritization instruction, kept here the way tech debt keeps
"Choosing what to fix": in this specification, which the Routine reads from `main` on
every run. Changing a priority is a change to this file.

Confirmed by the maintainer on 2026-10-19.

**What matters, in order.** Write each as a sentence or two a stranger could match an
issue against; the Routine quotes the part of an issue that made it match, so vague
themes produce vague matches.

1. **Winning the 6:30 race, and never losing a booking silently.** Anything that
   changes whether a member gets the tee time they asked for, or that leaves a booking
   stuck or unreported if the service crashes mid-run.
2. **Members can use the bot without the maintainer.** Joining, connecting a login,
   leaving, having their data removed, and not needing anyone's help to book.
3. **Members always know what happened.** Clear confirmations and failure messages,
   including why a time could not be booked.
4. **Cheap and boring to run.** Cost, infrastructure checks and anything that makes
   the deployment safer to change.
5. **Anything else a member asked for.**

Cleanup (lint suppressions, dead code, stale docs) is the tech-debt Routine's, not
this one's.

**Hold.** Issues or themes the Routine must not build, whatever their rank. An issue
on Hold stays ranked and shown; it is only removed from the build set. Closing a
Routine PR unmerged also stops that issue being rebuilt.

- *(none yet)*

**How the Routine uses it:**

- It matches each issue to the highest-ranked theme it plausibly belongs to and
  **quotes the sentence of the issue that made it match**. Matching is judgment, so
  the summary shows the quote and the theme next to every placement. An issue that
  fits no theme ranks last.
- Within a group, order by theme, then the maintainer's signal, then age.

## Ranking

Each run reads every open issue **not** labelled `tech-debt` (that backlog has its
own Routine), with comments, and for each records only things that can be looked up:

| Field | Source |
|---|---|
| Age, last activity | issue timestamps |
| Blocked by | `Blocked on`, `Depends on` or `#N` in the body that names an **open** issue. The text is quoted, not interpreted |
| Files it names | paths and identifiers in the body, checked against the tree. A path that does not exist is flagged |
| Sensitive area | whether a named path falls in the list in `tech-debt.md`, "Choosing what to fix" (racer, observer, booking path, `terraform/`, credentials, anything the 06:28 CT job runs) |
| Verification path | whether the body says how a fix would be checked. Absent is a fact, not a verdict |
| Open questions | decisions the body says are still needed, quoted |
| Existing PRs | whether an open PR already closes it, or an earlier `feature-dev` PR for it was closed unmerged |
| Priority theme | the theme from "Choosing what to build" it matches, with the quoted sentence; or none |
| Maintainer signal | the maintainer's own comments and reactions, which outrank everything above |

From those, the Routine places each issue in one group, with the reason on the same
line:

| Group | Means |
|---|---|
| **Ready to build** | nothing above blocks it, it names real files, it has a verification path, and it is outside the sensitive areas |
| **Needs a decision** | it lists an open question only the maintainer can settle |
| **Blocked** | it names an open issue as a prerequisite |
| **Needs triage** | the issue is not yet a task: it has no verification path and names no real file, or it is old with no activity, or it looks like a capture (screenshots, pasted bot output, several problems in one body). Stated as a hypothesis with the observation behind it, never as "this is stale" |
| **Sensitive** | it touches a sensitive area. Ranked last, but buildable: see below |

Within a group, order by priority theme (the order above), then the maintainer's
signal, then age. **The ordering is labelled a first guess.** It has no evidence
behind it, and the summary says so until there is some.

**Two things this section does not do.** It does not estimate effort or value: both
are judgment, and a number from a model here would read as measurement. And it does
not decide that an issue should be closed; it can say "no activity in N days", which
is a fact, and put it in *Needs triage* for the maintainer.

### Output

A summary printed as the session's final message, nothing posted to the repository,
in the same shape as tech debt's: one line per issue, the title as a link, a dash,
and one sentence, with its theme and the quoted sentence. Followed by:

- **Building this run**, or why nothing: the issue chosen and the single reason it is
  first.
- **Next up** (at most 3): the issues it would build next.
- **Established** and **not established**, in separate lists, for the ranking itself:
  what was looked up, and what is a guess.

It uses labels, never names, because the summary is written into a transcript.

## Building one issue

**Eligible** means: in *Ready to build*; not on the Hold list above; no open PR already
closing it; and no earlier `feature-dev` PR for it closed unmerged (the maintainer
already said no). Never one in *Needs a decision*, *Blocked* or *Needs triage*: an
unattended build from an under-specified issue is the failure this filter exists to
prevent. If nothing is eligible, the Routine builds nothing and says why.

**A *Sensitive* issue is eligible only when nothing in *Ready to build* is**, the
same rule as tech debt: deprioritize, do not forbid. Its PR says so in the first
lines, names what the 06:28 CT job executes that it changes, and says what to watch on
the next race morning. Two consequences worth stating: this project has no local way
to exercise booking ("testing means deploying"), so a green suite says little about a
booking-path change, and a change that touches `terraform/` applies on merge
(`cloudbuild.yaml` runs `terraform apply -auto-approve`). The PR states both.

It picks the highest-ranked eligible issue, and:

1. Comments on the issue *before touching code* with what it will change and how it
   will check it.
2. Branches `feature-dev/<issue number>-<slug>`, makes the change, and adds the
   tests that would have caught its absence.
3. **Updates `operations/features.md`** (including the "Why a user cares" cell) and
   the README's Features list if the change adds, retires or deletes a feature a
   member can see (`tests/test_feature_manifest.py` fails otherwise).
4. Runs the pre-push command in full and quotes its result. A failure means no PR.
5. Opens one non-draft PR with `Closes #<n>`, labelled `feature-dev`, with the same
   Risk, Review, Verified and Not verified sections `tech-debt.md` requires, and
   the same member-name check before any public text is created.
6. Triggers and answers CodeRabbit under the same budget as `tech-debt.md`,
   "CodeRabbit review".

**At most 2** open PRs from this Routine at a time, counted as open PRs labelled
`feature-dev`, lower than tech debt's 3. Features take longer to review than cleanup
does. At or over the cap it ranks and builds nothing.

**What merging does** is the maintainer's decision, as in `tech-debt.md`. A PR
touching app code redeploys the live service when merged; the PR says so, and says
the merge should not land between 06:15 and 06:45 CT. A README change redeploys too.

**To stop it building something,** add it to the Hold list above, or close its PR unmerged
(it will not be rebuilt). There is no label to manage.

## Authorization

| Can | Cannot |
|---|---|
| read the repository, GitHub and the ledgers | merge any PR, by any route |
| comment on issues, including to say an issue is blocked or why it was not built | close, reopen or edit an issue, or edit this specification |
| write one object to the `operations/feature-dev/` prefix in GCS | apply or remove any label other than `feature-dev` on its own PRs |
| push one branch and open one PR per run, and push further commits to it in response to review | push to `main`, or to a branch that is not its own |
| trigger CodeRabbit on its own PR, and reply to its findings there | trigger or reply on any other PR; edit another Routine's file or ledger |

This is not a standing authorization to write to `main`. CLAUDE.md names two
exceptions and this is not a third.

## The ledger row

One object per run, never an edit, per `operations/ledger/README.md`:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/feature-dev/<YYYY-MM-DD>.json
```

Write it with `gcloud storage cp --no-clobber`. The service account can create
objects under `operations/` and cannot overwrite them. If the write is refused
because today's object already exists — including because an earlier run today
failed and this is a maintainer-requested rerun — that object is the day's
record. Do the rerun's real work, but do not write a ledger row for it and do
not invent an alternate object name (`<date>-rerun.json` or similar) to get
around the refusal; say in the notification that the ledger already has today's
row instead (`operations/ledger/README.md`, "A same-day rerun does not get a
second row").

The values below are invented to show the shape.

```json
{"date":"2026-10-24","routine":"feature-dev","ok":true,
 "read":{"total_open":38,"open_issues":34,"excluded_tech_debt":4,"with_comments":34},
 "priorities":{"themes":5,"on_hold":2,"matched":21},
 "groups":{"ready":6,"needs_decision":9,"blocked":5,"needs_triage":8,"sensitive":6},
 "next_up":[309,134,276],
 "build":{"issue":134,"pr":330,"skipped":null,"sensitive_area":false,"open_prs_at_start":0},
 "review":{"requested":true,"rounds":1,"outcome":"reviewed","head_reviewed":true},
 "prior":{"prs_merged":0,"prs_closed_unmerged":0,"prs_open":1}}
```

`ok` describes the run, not the ranking. `read.open_issues` of 0 is a failed read,
`ok: false`, only when `read.total_open` shows open issues that are not labelled
`tech-debt`: this repository has been misled by a valid zero-row result before.
If every open issue is labelled `tech-debt`, an empty list is correct, and the run
is `ok: true` with nothing to rank. `build.skipped` is `null` when a PR was opened,
otherwise one of `no_eligible`, `cap`, `check_failed`, `error`.
Building nothing because nothing was eligible is `ok: true`.

**The measure that matters** is `prior.prs_closed_unmerged` against `prs_merged`,
both cumulative so a reader diffs consecutive rows. If the maintainer closes more
than about half of what this Routine opens, the priorities, the eligibility filter or
the PR quality is wrong, and that is the signal to change them.

## Auto-fix

**Established:** `autofix_on_pr_create` is on for this Routine's trigger (read from the
triggers API on 2026-10-20). **Expected, not observed:** that the desktop app then
watches each PR the Routine opens and, on a CI failure, a merge conflict or a review
comment, runs a fix session on that PR's branch, pushes, replies on the thread and
resolves it. The maintainer turned it on because the Routine's own session ends with
its run, and a failure or comment that arrives hours later would otherwise sit
unattended.

It is a second actor on the PR, so the rules differ from the Routine's:

- **Still bounded:** it never merges, and it pushes only to the PR's own branch. The
  maintainer reviews and merges every PR.
- **Not enforced on it:** the member-name check on the replies and commits it writes
  (they are public), the "judge each finding on its reasoning" rules, the rule not to
  widen the PR and to file out-of-scope findings as issues, and the full pre-push
  command before each push. Whether it follows them is not established. The
  maintainer's read of the PR is the only check on what it adds.
- **Threads:** the prompt says the Routine does not resolve threads; Auto-fix does.
  A reply still states what was done.
- **The ledger:** the run's `review` fields describe the Routine's session only.
  Commits Auto-fix adds afterwards are visible on the PR and are not in the row, so
  `head_reviewed` can be stale; the PR states the sha CodeRabbit reviewed.
- **Not established:** how Auto-fix behaves on a PR opened by a Routine session. It has
  not been observed.

## No DST defect

This trigger's cron carries `CRON_TZ=America/Chicago`, so it stays at 08:00 CT across
the 2026-11-01 change, unlike the UTC crons of the race report, scoreboard and tech
debt. Tech debt's `0 9 * * 6` UTC moves from 04:00 CDT to 03:00 CST that day, which
only widens the gap between the two.

## Notification

Every run, since a weekly cadence makes silence ambiguous. One or two sentences, no
markdown, labels not names. For example:
`Feature dev - 34 open issues ranked, 6 ready, 8 need triage. PR #330 opened for #134, merge redeploys.`
`Feature dev - 34 open issues ranked. Nothing eligible to build: 3 are blocked on an open issue, the rest need a decision.`

## Not established

- **That a model's reading of an issue body is reliable enough to group on.** The
  fields above are quotations and lookups on purpose. Whether the *Ready to build*
  group is right is the thing the first weeks measure. Expect the first rankings to
  be wrong in ways that look reasonable.
- **That matching an issue to a priority theme is reliable.** It is judgment. The
  quoted sentence next to each match is how the maintainer catches a wrong one.
- **Whether the eligibility filter is strict enough.** Without a per-issue release,
  the filter is the only thing between an under-specified issue and a PR. The PR cap
  and the maintainer's review bound the cost of a miss; they do not prevent it.
- **Whether a Routine session can schedule a wake-up** for a CodeRabbit retry. The
  same open question as `tech-debt.md`, "Open items". The tech-debt Routine's
  2026-10-17 row shows a CodeRabbit review landing from a Routine session.
- **How many of the 50 are features.** The count is of all open issues. The split
  between features, bugs, decisions and stale items has not been made.

## Decisions

Settled 2026-10-17: build is in scope from the start; sensitive issues are buildable
and ranked last; weekly, 08:00 CT (written as Sunday here, created as Saturday by the
maintainer on 2026-10-19; the trigger is the record); and issues that are not tasks land in
*Needs triage*, not on an exclude list.

Settled 2026-10-18: **no release label.** The maintainer prioritizes through
"Choosing what to build" in this file; the Routine builds the best eligible issue itself, and the
maintainer's review and merge are the gate. This also removes the question of whether
the Routine's GitHub account could be told apart from the maintainer's: the tech-debt
Routine's issues and PRs are all authored by the maintainer's account, so it could
not have been.

Confirmed 2026-10-19: the priorities in "Choosing what to build" are the maintainer's.
No design decisions are open.

## The prompt

Paste everything inside the fence. Like the other prompts it avoids paired asterisks,
which the routines UI would read as markdown, and keeps a blank line between every
two lines, because the UI joins single line breaks. Compare the live text after
pasting. **Drafted 2026-10-17, revised 2026-10-18; not pasted anywhere, and not yet
run.**

```
You are running the weekly feature-development Routine for the TeeTime booking bot. You start with zero context; everything you need is in this repo. Read operations/routines/feature-dev.md first: it is the specification, and it governs where this prompt is silent. Then read operations/routines/tech-debt.md, sections CodeRabbit review, Every issue and every PR carries risk and verification, and The member-name check, which this Routine follows unchanged. If either file does not exist, write the ledger row with ok false and the note spec missing, notify, and stop.

You rank the open backlog by the maintainer's priorities, and you build at most one eligible issue per run. You never merge anything, ever. You are not authorized to push to main, to merge your own PR, to close or edit an issue, to edit the specification, or to apply or remove any label other than feature-dev on your own PRs.

Rule that applies to every path through this prompt: always write your ledger row, then always send a push notification, in that order. Whatever happens, finish by writing one object to gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/feature-dev/ named for the run date in Central Time, never overwriting, and then calling PushNotification. If that write is refused because today's object already exists, including because this is a maintainer-requested rerun after an earlier failure today, do not write under a different name instead; that object already is the day's record, so say so in the notification and move on. Every stop in this prompt means ledger row, notification, summary.

Rule that applies to every path through this prompt: no names. Issues, comments, PR titles and bodies, commit messages and branch names are public. Before creating or editing any of them, write the text to a file and run scripts/check_report_names.py against it with --labels ~/.teetime/labels.json, reading the labels from MEMBER_PSEUDONYM_LABELS as tech-debt.md, The member-name check, describes. Exit 0 is the only pass. If the check cannot run, create nothing public and mark the run ok false. Refer to people only by label, and never copy a name from an issue into anything you write.

Step 1 - set up. Get today's date with TZ=America/Chicago date. Run bash scripts/setup_remote_env.sh. Only READY is a pass. On anything else, write the ledger row with ok false naming the failed path, notify, and stop. Make sure the label feature-dev exists and create it if it is missing.

Step 2 - read the backlog. List every open issue that is not labelled tech-debt, with bodies and comments. Also list all open issues without the filter. If the filtered list is empty while some open issue is not labelled tech-debt, the read failed: ok false, say so, stop. If every open issue is labelled tech-debt, the empty list is correct: rank nothing and continue. List PRs labelled feature-dev and count, cumulatively, how many are merged, closed without merging, or open.

Step 3 - rank. For each issue record only what can be looked up: age and last activity; any open issue it names as a prerequisite, quoting the text; the paths and identifiers it names, checked against the tree, flagging any that do not exist; whether a path falls in the sensitive areas, read fresh from tech-debt.md, Choosing what to fix; whether it says how a fix would be checked; open questions it lists, quoted; whether an open PR already closes it or an earlier feature-dev PR for it was closed unmerged; and the maintainer's own comments and reactions. Match each issue to the highest theme in the specification section Choosing what to build that it plausibly belongs to and quote the sentence of the issue that made it match; if none fits, say none. Place each issue in exactly one group: Ready to build, Needs a decision, Blocked, Needs triage, or Sensitive, as the specification defines them. Within a group order by theme, then the maintainer's signal, then age. Do not estimate effort or value. Do not say an issue should be closed; for an old or capture-like issue say what you observed and put it in Needs triage. State that the ordering is a first guess.

Step 4 - build, if clear. Count open PRs labelled feature-dev. If there are 2 or more, build nothing and record skipped as cap. An issue is eligible only if it is in Ready to build, is not on the Hold list in the specification section Choosing what to build, has no open PR closing it, and has no earlier feature-dev PR closed unmerged. Never take an issue from Needs a decision, Blocked or Needs triage. Take the highest-ranked eligible issue. Only if no Ready to build issue is eligible may you take a Sensitive one, and then the PR says so in its first lines. If nothing is eligible, build nothing and record skipped as no_eligible.

Before touching code, comment on the issue with what you will change and how you will check it. Create the branch feature-dev/<issue number>-<slug>. Make the change and add the tests that would have caught its absence. If the change adds, retires or deletes a feature, update operations/features.md, including its Why a user cares cell, in the same change, and update the README Features list if a member can see the difference; the test tests/test_feature_manifest.py fails otherwise. Read the `coverage` artifact of the latest green `Tests` run on `main` as a per-file baseline only and add tests for the lines the change touches. CI also fails the `Check coverage ratchet` step when the count of uncovered code is more than 5 above or below `operations/coverage-ratchet.txt`; Step 4a says what to do about that after the PR is open. Run the full pre-push command and quote its result: poetry run pytest -q, poetry run ruff check ., poetry run ruff format --check ., poetry run mypy app, all of them. If any fails, do not open the PR; record skipped as check_failed.

Open a normal PR into main with the feature-dev label and Closes #<n>. The first section of the body is Risk, as tech-debt.md requires: the files and whether any is a sensitive area; whether merging redeploys the service or applies terraform, determined by comparing the changed paths with the Cloud Build ignored_files list read fresh from terraform/main.tf; whether anything the 06:28 CT job executes changed and what to watch on the next race morning; and that a merge which redeploys should not land between 06:15 and 06:45 CT. If the issue is in a sensitive area, say so in the first lines. Then Review, Verified, and Not verified sections, in that order. The pre-push result is quoted in Verified; what the tests do not cover is stated in Not verified, and this project has no local way to exercise booking.

Step 4a - coverage ratchet. Opening the PR starts its Tests run; the workflow does not run on a branch push alone, so do this only after the PR is open. Wait for the PR's Tests run. Read its `coverage` artifact to check the lines this change touches; if it is unavailable, say in the PR's Not verified section that changed-line coverage was not verified. If its Check coverage ratchet step fails with "fewer than the ratchet", write the value it prints to operations/coverage-ratchet.txt and push. If it fails with "more than the ratchet", do not write the file: add tests for the new or changed code and push. Never compute that value from a local run.

Step 4b - CodeRabbit. Read .claude/skills/ship-pr/SKILL.md and follow its sections on triggering, rate limits, reading the review and judging each comment, and the budget in tech-debt.md, CodeRabbit review. Post a comment on your PR reading @coderabbitai review - new commit <short sha>, wait until its reply has stopped changing for 30 seconds, and classify it. If it refuses, stop trying, leave the PR open, say at the top of the PR that CodeRabbit did not review it, and record review outcome rate_limited. If it reviews, judge each finding on its reasoning, fix what is real and in scope with a test, reply to the rest, and file out-of-scope findings as new issues after the name check. Never post a second trigger for a commit it has already seen. At most two rounds.

Step 5 - ledger row, then notification, then summary. Write the ledger row with the shape in the specification: ok describes the run, and a run that ranked honestly and built nothing is ok true. Then push a notification of one or two sentences, no markdown, labels not names: how many issues were ranked, how many are ready or need triage, and either the PR number with whether merging redeploys or why nothing was built.

Last, as the final message of this session, print the summary: a heading with the date; the groups, each issue on one line as the title linked to the issue followed by a dash and one sentence saying what it is, its theme with the quoted sentence, and why it is in that group; Building this run, or why nothing; Next up, at most 3; and two lists, Established and Not established, for the ranking itself. Use labels, never names. If there is nothing open, print Nothing to rank, not nothing at all.
```

## Deploying it

Who does each step:

| # | Step | Who |
|---|---|---|
| 1 | Confirm the themes and Hold list in "Choosing what to build" | **Done 2026-10-19** |
| 2 | Correct the groups, the eligibility filter and the sensitive-area list if they are wrong | **You** |
| 3 | Merge the spec and prompt (#322) | **Done 2026-10-19** |
| 4 | Create the Routine, paste the prompt, compare the live text with this file, record the trigger ID | **Done 2026-10-19 / 2026-10-20**: created by the maintainer, compared by Claude, ID recorded above |
| 5 | Add the Routine to `SCHEDULES` in `operations/ledger/derive_scoreboard.py` and to `operations/scoreboard.md` section 2: weekly, Saturday 08:00 CT, from 2026-10-24. A scheduled date with no row breaks the streak | **Claude**, in the open PR #328, to merge before the first fire |
| 6 | Add the ledger prefix to `operations/ledger/README.md` and the Routine to `CLAUDE.md`'s list | **Done in this PR** |
| 7 | Watch the first run, Saturday 2026-10-24 at 08:00 CT: the first test of the grouping, of building from a ranking, and of Auto-fix on a Routine's PR | **You** |
