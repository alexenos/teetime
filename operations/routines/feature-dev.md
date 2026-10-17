# Routine: feature development

**Status: proposed, scope approved by the maintainer 2026-10-17. Not deployed. The prompt
is drafted below and has not been run.** This file is the specification for the maintainer to
correct; all of its design decisions are settled (see "Decisions").

| | |
|---|---|
| **Trigger** | weekly, Sunday `0 13 * * 0` UTC (08:00 CDT; 07:00 CST after 2026-11-01, the same DST defect the other Routines carry). A different day and hour from tech debt (Saturday 04:00 CDT), so the two never compete for the hourly CodeRabbit quota, and well clear of the 06:28 CT job |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Authorization** | reads the repository and GitHub. Comments on issues. Pushes one branch and opens one PR per run, for an issue the maintainer has released. Writes one ledger object to GCS. **Never merges.** |
| **Emits** | a ranked list of the open backlog in the session summary; at most one PR per run; one ledger object per run at `operations/feature-dev/<YYYY-MM-DD>.json` in GCS |
| **Owns** | the ranking, and its own run metadata. **Not** the decision about what gets built |

## What it is for

The backlog held 50 open issues on 2026-10-17, most of them features, fixes and
decisions rather than lint. [`tech-debt.md`](tech-debt.md) works because its signal is
checkable: a `noqa`, a stale path or a skipped test either exists or does not.
"What should be built next" is not checkable, and this repository has misdiagnosed
three times from a signal that looked well-formed and carried no information. So
this Routine is built to keep the *decision* with the maintainer and give the
agent the two jobs it can do without a judgment call becoming a deployment:

1. **Rank.** Read every open issue, state what is known about each, and order them,
   with the reason.
2. **Build what the maintainer has released.** Take one issue the maintainer has
   marked ready and open a PR for it.

## The maintainer decides what is built

The mechanism is a GitHub label, **`ready-for-agent`**. Applying it to an issue is
how the maintainer says "build this one". The Routine never applies, removes or
edits it, and an issue without it is ranked and never built. The label is the whole
of the maintainer's control over the build queue, so it has to mean one thing.

**Who may apply it:** only the maintainer, settled 2026-10-17. What the repository
cannot answer is whether that can be enforced. The Routine checks the
issue's `labeled` events for the actor, and ignores the label when the actor is
anyone else. But Routine sessions probably act through the maintainer's own GitHub
credentials, which would make the Routine and the maintainer the same actor to
GitHub. In that case the check cannot tell them apart, and the real guarantee is
the Routine's instruction never to touch the label, not an identity check. **Not
established.** If the actor cannot be read at all, the Routine ranks and builds
nothing, and records `build.skipped: "actor_unreadable"` rather than trusting a
label it cannot attribute.

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
| Maintainer signal | the maintainer's own comments, reactions and labels, which outrank everything above |

From those, the Routine places each issue in one group, with the reason on the same
line:

| Group | Means |
|---|---|
| **Ready to build** | nothing above blocks it, it names real files, it has a verification path, and it is outside the sensitive areas |
| **Needs a decision** | it lists an open question only the maintainer can settle |
| **Blocked** | it names an open issue as a prerequisite |
| **Needs triage** | the issue is not yet a task: it has no verification path and names no real file, or it is old with no activity, or it looks like a capture (screenshots, pasted bot output, several problems in one body). Stated as a hypothesis with the observation behind it, never as "this is stale" |
| **Sensitive** | it touches a sensitive area. Ranked last, but buildable: see below |

Within a group, order by the maintainer's signal, then age. **The ordering is
labelled a first guess.** It has no evidence behind it, and the summary says so
until there is some.

**Two things this section does not do.** It does not estimate effort or value: both
are judgment, and a number from a model here would read as measurement. And it does
not decide that an issue should be closed; it can say "no activity in N days", which
is a fact, and put it in *Needs triage* for the maintainer.

### Output

A summary printed as the session's final message, nothing posted to the repository,
in the same shape as tech debt's: one line per issue, the title as a link, a dash,
and one sentence. Followed by:

- **Suggested for `ready-for-agent`** (at most 3): issues in *Ready to build*, each
  with the single reason it is first. This is the Routine's whole influence on the
  queue.
- **Established** and **not established**, in separate lists, for the ranking itself:
  what was looked up, and what is a guess.

It uses labels, never names, because the summary is written into a transcript.

## Building one released issue

After ranking, the Routine considers issues carrying a maintainer-applied
`ready-for-agent`. It picks one, preferring the highest-ranked, and:

1. **Skips a released issue it can see is blocked**, and comments on the issue
   saying which open issue blocks it. A label cannot release an issue whose
   prerequisite is open.
2. Comments on the issue *before touching code* with what it will change and how it
   will check it.
3. Branches `feature-dev/<issue number>-<slug>`, makes the change, and adds the
   tests that would have caught its absence.
4. **Updates `operations/features.md`** if the change adds, retires or deletes a
   feature (`tests/test_feature_manifest.py` fails otherwise).
5. Runs the pre-push command in full and quotes its result. A failure means no PR.
6. Opens one non-draft PR with `Closes #<n>`, labelled `feature-dev`, with the same
   Risk, Review, Verified and Not verified sections `tech-debt.md` requires, and
   the same member-name check before any public text is created.
7. Triggers and answers CodeRabbit under the same budget as `tech-debt.md`,
   "CodeRabbit review".

**Sensitive issues are buildable, and ranked last.** The same rule as tech debt:
deprioritize, do not forbid. A released issue in a sensitive area is built when it
is released, and its PR says so in its first lines, names what the 06:28 CT job
executes that it changes, and says what to watch on the next race morning. The
maintainer applying the label is the decision to take that risk; the Routine adds
no second gate. Two consequences worth stating: this project has no local way to
exercise booking ("testing means deploying"), so a green suite says little about a
booking-path change, and a change that touches `terraform/` applies on merge
(`cloudbuild.yaml` runs `terraform apply -auto-approve`). The PR states both.

**At most 2** open PRs from this Routine at a time, counted as open PRs labelled
`feature-dev`, lower than tech debt's 3. Features take longer to review than cleanup
does. At or over the cap it ranks and builds nothing.

**What merging does** is the maintainer's decision, as in `tech-debt.md`. A PR
touching app code redeploys the live service when merged; the PR says so, and says
the merge should not land between 06:15 and 06:45 CT.

## Authorization

| Can | Cannot |
|---|---|
| read the repository, GitHub, the ledgers and issue events | merge any PR, by any route |
| comment on issues, including to say an issue is blocked or why it was not built | apply, remove or alter `ready-for-agent`, or any other label it did not create |
| write one object to the `operations/feature-dev/` prefix in GCS | close, reopen or edit an issue |
| push one branch and open one PR per run, and push further commits to it in response to review | push to `main`, or to a branch that is not its own |
| trigger CodeRabbit on its own PR, and reply to its findings there | trigger or reply on any other PR; edit another Routine's file or ledger |

This is not a standing authorization to write to `main`. CLAUDE.md names two
exceptions and this is not a third.

## The ledger row

One object per run, never an edit, per `operations/ledger/README.md`:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/feature-dev/<YYYY-MM-DD>.json
```

The values below are invented to show the shape.

```json
{"date":"2026-10-25","routine":"feature-dev","ok":true,
 "read":{"open_issues":34,"excluded_tech_debt":4,"with_comments":34},
 "groups":{"ready":6,"needs_decision":9,"blocked":5,"needs_triage":8,"sensitive":6},
 "suggested":[309,134,276],
 "released":{"labelled":2,"maintainer_applied":2,"ignored_label":0},
 "build":{"issue":134,"pr":330,"skipped":null,"sensitive_area":false,"open_prs_at_start":0},
 "prior":{"suggested_then_labelled":1,"prs_merged":0,"prs_closed_unmerged":0,"prs_open":1}}
```

`ok` describes the run, not the ranking. `read.open_issues` of 0 on a backlog that is
not empty is a failed read, `ok: false`: this repository has been misled by a valid
zero-row result before. `released.ignored_label` counts issues carrying the label
from an actor the Routine would not accept. `build.skipped` is `null` when a PR was
opened, otherwise one of `no_released`, `blocked`, `cap`, `actor_unreadable`,
`check_failed`, `error`. Having nothing released is `ok: true`.

**The measures that matter**, both cumulative so a reader diffs consecutive rows:

- **`prior.suggested_then_labelled`**: how many issues the Routine suggested that the
  maintainer then released. Low means the ranking is not telling the maintainer
  anything.
- **`prs_closed_unmerged` against `prs_merged`**. If the maintainer closes more than
  about half of what this Routine opens, the ranking or the PR quality is wrong.

## Notification

Every run, since a weekly cadence makes silence ambiguous. One or two sentences, no
markdown, labels not names. For example:
`Feature dev - 34 open issues ranked, 6 ready, 8 need triage. Suggested for release: 3. PR #330 opened for #134, merge redeploys.`

## Not established

- **That the label's actor is readable, and distinguishable from the Routine.** See
  "The maintainer decides what is built". If it cannot be read, nothing is built.
- **That a model's reading of an issue body is reliable enough to group on.** The
  fields above are quotations and lookups on purpose. Whether the *Ready to build*
  group is right is the thing the first weeks measure. Expect the first rankings to
  be wrong in ways that look reasonable.
- **Whether a Routine session can schedule a wake-up** for a CodeRabbit retry. The
  same open question as `tech-debt.md`, "Open items".
- **How many of the 50 are features.** The count is of all open issues. The split
  between features, bugs, decisions and stale items has not been made.

## Decisions

Settled 2026-10-17: build is in scope from the start; sensitive issues are buildable
and ranked last; weekly, Sunday, 08:00 CT; only the maintainer's `ready-for-agent`
counts, with no collaborator exception; and issues that are not tasks are not
excluded by a label. They land in *Needs triage* with the observation behind them,
and the maintainer splits or closes them.

**No decisions are open.** What remains is the prompt, and the unverified items in
"Not established".

## The prompt

Paste everything inside the fence. Like the other prompts it avoids paired asterisks,
which the routines UI would read as markdown, and keeps a blank line between every
two lines, because the UI joins single line breaks. Compare the live text after
pasting. **Drafted 2026-10-17; not pasted anywhere, and not yet run.**

```
You are running the weekly feature-development Routine for the TeeTime booking bot. You start with zero context; everything you need is in this repo. Read operations/routines/feature-dev.md first: it is the specification, and it governs where this prompt is silent. Then read operations/routines/tech-debt.md, sections CodeRabbit review, Every issue and every PR carries risk and verification, and The member-name check, which this Routine follows unchanged. If either file does not exist, write the ledger row with ok false and the note spec missing, notify, and stop.

You rank the open backlog, and you build at most one issue that the maintainer has released. You never merge anything, ever. You are not authorized to push to main, to merge your own PR, to close or edit an issue, or to apply, remove or change the ready-for-agent label or any label you did not create.

Rule that applies to every path through this prompt: always write your ledger row, then always send a push notification, in that order. Whatever happens, finish by writing one object to gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/feature-dev/ named for the run date in Central Time, never overwriting, and then calling PushNotification. Every stop in this prompt means ledger row, notification, summary.

Rule that applies to every path through this prompt: no names. Issues, comments, PR titles and bodies, commit messages and branch names are public. Before creating or editing any of them, write the text to a file and run scripts/check_report_names.py against it with --labels ~/.teetime/labels.json, reading the labels from MEMBER_PSEUDONYM_LABELS as tech-debt.md, The member-name check, describes. Exit 0 is the only pass. If the check cannot run, create nothing public and mark the run ok false. Refer to people only by label, and never copy a name from an issue into anything you write.

Step 1 - set up. Get today's date with TZ=America/Chicago date. Run bash scripts/setup_remote_env.sh. Only READY is a pass. On anything else, write the ledger row with ok false naming the failed path, notify, and stop. Make sure the label feature-dev exists and create it if it is missing. Do not create ready-for-agent: only the maintainer creates and applies it.

Step 2 - read the backlog. List every open issue that is not labelled tech-debt, with bodies and comments. If the list is empty while the repository has open issues, the read failed: ok false, say so, stop. List PRs labelled feature-dev and count, cumulatively, how many are merged, closed without merging, or open.

Step 3 - rank. For each issue record only what can be looked up: age and last activity; any open issue it names as a prerequisite, quoting the text; the paths and identifiers it names, checked against the tree, flagging any that do not exist; whether a path falls in the sensitive areas, read fresh from tech-debt.md, Choosing what to fix; whether it says how a fix would be checked; open questions it lists, quoted; and the maintainer's own comments, reactions and labels. Place each issue in exactly one group: Ready to build, Needs a decision, Blocked, Needs triage, or Sensitive, as the specification defines them. Sensitive issues are ranked last. Within a group order by the maintainer's signal, then age. Do not estimate effort or value. Do not say an issue should be closed; for an old or capture-like issue say what you observed and put it in Needs triage. State that the ordering is a first guess.

Step 4 - find released issues. For each open issue labelled ready-for-agent, read its issue events and find the labeled event for that label. Accept the label only if the actor is the repository owner. If you cannot read events at all, build nothing and record build skipped as actor_unreadable. Count labelled, accepted, and ignored.

Step 5 - build, if clear. Count open PRs labelled feature-dev. If there are 2 or more, build nothing and record skipped as cap. Otherwise take the highest-ranked accepted issue. If it names an open prerequisite, do not build it: comment on that issue naming the blocker, record skipped as blocked, and consider the next. If none is accepted, record skipped as no_released.

Before touching code, comment on the issue with what you will change and how you will check it. Create the branch feature-dev/<issue number>-<slug>. Make the change and add the tests that would have caught its absence. If the change adds, retires or deletes a feature, update operations/features.md in the same change; the test tests/test_feature_manifest.py fails otherwise. Run the full pre-push command and quote its result: poetry run pytest -q, poetry run ruff check ., poetry run ruff format --check ., poetry run mypy app, all of them. If any fails, do not open the PR; record skipped as check_failed.

Open a normal PR into main with the feature-dev label and Closes #<n>. The first section of the body is Risk, as tech-debt.md requires: the files and whether any is a sensitive area; whether merging redeploys the service or applies terraform, determined by comparing the changed paths with the Cloud Build ignored_files list read fresh from terraform/main.tf; whether anything the 06:28 CT job executes changed and what to watch on the next race morning; and that a merge which redeploys should not land between 06:15 and 06:45 CT. If the issue is in a sensitive area, say so in the first lines. Then Review, Verified, and Not verified sections, in that order. The pre-push result is quoted in Verified; what the tests do not cover is stated in Not verified, and this project has no local way to exercise booking.

Step 5b - CodeRabbit. Read .claude/skills/ship-pr/SKILL.md and follow its sections on triggering, rate limits, reading the review and judging each comment, and the budget in tech-debt.md, CodeRabbit review. Post a comment on your PR reading @coderabbitai review - new commit <short sha>, wait until its reply has stopped changing for 30 seconds, and classify it. If it refuses, stop trying, leave the PR open, say at the top of the PR that CodeRabbit did not review it, and record review outcome rate_limited. If it reviews, judge each finding on its reasoning, fix what is real and in scope with a test, reply to the rest, and file out-of-scope findings as new issues after the name check. Never post a second trigger for a commit it has already seen. At most two rounds.

Step 6 - ledger row, then notification, then summary. Write the ledger row with the shape in the specification: ok describes the run, and a run that ranked honestly and built nothing is ok true. Then push a notification of one or two sentences, no markdown, labels not names: how many issues were ranked, how many are ready or need triage, how many you suggest for release, and either the PR number with whether merging redeploys or why nothing was built.

Last, as the final message of this session, print the summary: a heading with the date; the groups, each issue on one line as the title linked to the issue followed by a dash and one sentence saying what it is and why it is in that group; Suggested for ready-for-agent, at most 3, each with the single reason it is first; the PR line, or why there is none; and two lists, Established and Not established, for the ranking itself. Use labels, never names. If there is nothing open, print Nothing to rank, not nothing at all.
```

## Deploying it

Nothing is built. When the decisions above are made:

| # | Step | Who |
|---|---|---|
| 1 | Correct the groups and the sensitive-area list if they are wrong | **You** |
| 2 | The prompt is written above | **Done 2026-10-17**, unrun. **You** read and correct it |
| 3 | Create the `ready-for-agent` and `feature-dev` labels | **Claude**, when asked |
| 4 | Create the Routine, paste the prompt, compare the live text with this file, record the trigger ID | **You**; Claude can do the comparison |
| 5 | Add the ledger prefix to `operations/ledger/README.md` and the Routine to `CLAUDE.md`'s list | **Claude**, in the PR that deploys it |
| 6 | Watch the first run: it is the first test of the label-actor check and of building from a ranking | **You** |
