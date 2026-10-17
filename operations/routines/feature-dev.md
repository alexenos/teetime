# Routine: feature development

**Status: proposed, scope approved by the maintainer 2026-10-17. Not deployed, and
no prompt is written yet.** This file is the specification for the maintainer to
correct. The prompt is written once the open decisions at the bottom are settled.

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

**Who may apply it is a question this spec cannot fully answer from the
repository.** The intent is that only the maintainer does. The Routine checks the
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

## Decisions for the maintainer

Settled 2026-10-17: build is in scope from the start; sensitive issues are buildable
and ranked last; weekly, Sunday, 08:00 CT.

1. **Who may apply `ready-for-agent`.** Presumably only you. Say if a collaborator
   should also count. See the caveat above on whether the check can tell you from
   the Routine.
2. **What to do with issues that are not tasks**, such as one that bundles several
   problems under screenshots, or one holding pasted bot output. The spec has no
   exclude list; they land in *Needs triage* with the observation behind it, and you
   split or close them. Say if you would rather they were labelled out of the read.

## Deploying it

Nothing is built. When the decisions above are made:

| # | Step | Who |
|---|---|---|
| 1 | Answer the two decisions; correct the groups and the sensitive-area list if they are wrong | **You** |
| 2 | Write the prompt in this file, in the shape of `tech-debt.md`, "The prompt" | **Claude** |
| 3 | Create the `ready-for-agent` and `feature-dev` labels | **Claude**, when asked |
| 4 | Create the Routine, paste the prompt, compare the live text with this file, record the trigger ID | **You**; Claude can do the comparison |
| 5 | Add the ledger prefix to `operations/ledger/README.md` and the Routine to `CLAUDE.md`'s list | **Claude**, in the PR that deploys it |
| 6 | Watch the first run: it is the first test of the label-actor check and of building from a ranking | **You** |
