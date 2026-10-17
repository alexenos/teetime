# Routine: feature development

**Status: proposed. Not deployed, and no prompt is written yet.** This file is the
specification for the maintainer to correct. The prompt is written after the
decisions at the bottom are made, because several of them change what it says.

| | |
|---|---|
| **Trigger** | weekly, proposed Sunday `0 9 * * 0` UTC (04:00 CDT, 03:00 CST). A different day from tech debt (Saturday), so the two never compete for the hourly CodeRabbit quota |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Authorization, version 1** | reads the repository and GitHub. Comments on issues. Writes one ledger object to GCS. **Opens no PR and changes no label.** |
| **Authorization, version 2** | adds: pushes one branch and opens one PR per run, for an issue the maintainer has marked ready. **Never merges.** |
| **Emits** | a ranked list of the open backlog in the session summary; in version 2, at most one PR per run; one ledger object per run at `operations/feature-dev/<YYYY-MM-DD>.json` in GCS |
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
2. **Build, later, and only what the maintainer has released.** Take one issue the
   maintainer has marked ready and open a PR for it.

Version 1 is job 1 alone. Version 2 adds job 2 once version 1's ranking has been
read for a few weeks and found worth trusting.

## The maintainer decides what is ready

An issue is buildable by this Routine only if it carries the label
**`ready-for-agent`** *and* the maintainer applied it. The Routine checks who: the
issue's events list the label's `labeled` event with an actor, and an actor other
than the repository owner means the label is ignored. It never applies
`ready-for-agent` itself, and it reports an issue it would have built but cannot as
a *suggestion* in the summary, which is the whole of its influence on the queue.

That is the boundary a checkable fact can hold. "The Routine thinks this is a good
idea" is not one.

## Version 1: ranking

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

From those, the Routine places each issue in one of four groups, with the reason on
the same line:

| Group | Means |
|---|---|
| **Ready to build** | nothing above blocks it, it names real files, it has a verification path, and it is outside the sensitive areas |
| **Needs a decision** | it lists an open question only the maintainer can settle |
| **Blocked** | it names an open issue as a prerequisite |
| **Sensitive** | it touches a sensitive area. It may still be worth doing; it is never something the Routine builds on its own |

Within a group, order by the maintainer's signal, then age. **The ordering is
labelled a first guess.** It has no evidence behind it, and the summary says so
until there is some.

**Two things this section does not do.** It does not estimate effort or value: both
are judgment, and a number from a model here would read as measurement. And it does
not decide that an issue is stale or should be closed; it can say "no activity in
N days", which is a fact.

### Output

A summary printed as the session's final message, nothing posted to the repository,
in the same shape as tech debt's: one line per issue, the title as a link, a dash,
and one sentence. Followed by:

- **Suggested for `ready-for-agent`** (at most 3): issues in *Ready to build*, each
  with the single reason it is first.
- **Established** and **not established**, in separate lists, for the ranking itself:
  what was looked up, and what is a guess.

It uses labels, never names, because the summary is written into a transcript.

## Version 2: building one released issue

Taken only when version 1 has run for several weeks **and the maintainer says go**.
It would add, to a run:

1. Choose among issues carrying a maintainer-applied `ready-for-agent`, in
   *Ready to build* or *Needs a decision* resolved by a maintainer comment. Never
   one in *Sensitive* or *Blocked*, even if labelled: a label cannot release an
   issue the Routine can see is blocked, and it says so on the issue.
2. Branch `feature-dev/<issue number>-<slug>`, make the change, add the tests that
   would have caught its absence, and **update `operations/features.md`** if it adds,
   retires or deletes a feature (`tests/test_feature_manifest.py` fails otherwise).
3. Run the pre-push command in full and quote its result. A failure means no PR.
4. Open one non-draft PR with `Closes #<n>`, labelled `feature-dev`, with the same
   Risk, Review, Verified and Not verified sections `tech-debt.md` requires, and the
   same member-name check before any public text is created.
5. Trigger and answer CodeRabbit under the same budget as `tech-debt.md`,
   "CodeRabbit review".

**Stricter than tech debt on purpose.** Tech debt *deprioritizes* the sensitive
areas; this Routine *never builds* in them, because a feature change in the booking
path is a behavior change, and "testing means deploying" here: there is no local way
to exercise booking, so a green suite says little about it. A feature that needs a
terraform flag also falls out, since terraform is sensitive.

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
| *(version 2)* push one branch and open one PR per run, and push further commits to it in response to review | push to `main`, or to a branch that is not its own |
| *(version 2)* trigger CodeRabbit on its own PR | trigger or reply on any other PR; edit another Routine's file or ledger |

This is not a standing authorization to write to `main`. CLAUDE.md names two
exceptions and this is not a third.

## The ledger row

One object per run, never an edit, per `operations/ledger/README.md`:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/feature-dev/<YYYY-MM-DD>.json
```

The values below are invented to show the shape.

```json
{"date":"2026-10-18","routine":"feature-dev","ok":true,"version":1,
 "read":{"open_issues":34,"excluded_tech_debt":4,"with_comments":34},
 "groups":{"ready":6,"needs_decision":9,"blocked":5,"sensitive":14},
 "suggested":[309,134,276],
 "released":{"labelled":2,"maintainer_applied":2,"ignored_label":0},
 "build":{"issue":null,"pr":null,"skipped":"version_1"},
 "prior":{"suggested_then_labelled":1,"prs_merged":0,"prs_closed_unmerged":0,"prs_open":0}}
```

`ok` describes the run, not the ranking. `read.open_issues` of 0 on a backlog that is
not empty is a failed read, `ok: false`: this repository has been misled by a valid
zero-row result before. `released.ignored_label` counts issues carrying the label
from someone other than the maintainer.

**The measures that matter**, both cumulative so a reader diffs consecutive rows:

- **`prior.suggested_then_labelled`**: how many issues the Routine suggested that the
  maintainer then released. Low means the ranking is not telling the maintainer
  anything. This is the version 1 signal.
- **`prs_closed_unmerged` against `prs_merged`** (version 2). If the maintainer
  closes more than about half of what this Routine opens, the selection or the
  boundary is wrong.

## Notification

Every run, since a weekly cadence makes silence ambiguous. One or two sentences, no
markdown, labels not names. For example:
`Feature dev - 34 open issues ranked, 6 ready, 9 need a decision, 14 sensitive. Suggested for release: 3.`

## Not established

- **That a model's reading of an issue body is reliable enough to group on.** The
  fields above are quotations and lookups on purpose. Whether the *Ready to build*
  group is right is the thing the first weeks measure. Expect the first rankings to
  be wrong in ways that look reasonable.
- **That `ready-for-agent` events carry an actor a Routine session can read.** GitHub
  records one, but this has not been run from a Routine environment, and the
  environment's token scope is not checked. If it cannot, version 2 cannot start.
- **Whether a Routine session can schedule a wake-up** for a CodeRabbit retry. The
  same open question as `tech-debt.md`, "Open items".
- **How many of the 50 are features.** The count is of all open issues. The split
  between features, bugs, decisions and stale items has not been made.

## Decisions for the maintainer

1. **Version 1 only, to start?** Recommended: yes. Nothing here can change the repository.
2. **The label name and owner.** `ready-for-agent` applied by the repository owner.
   Say if there should be more than one person.
3. **Should *Sensitive* issues ever be buildable**, with a second, explicit label
   (for example `agent-ok-sensitive`)? Recommended: no, not in version 2.
4. **Weekly on Sunday**, or should ranking run more often than building? Ranking is
   cheap, building is not.
5. **Issues this Routine should never read**, such as `#107`, which appears to be a
   conversation log. Excluded by label or by number.

## Deploying it

Nothing is built. When the decisions above are made:

| # | Step | Who |
|---|---|---|
| 1 | Answer the five decisions; correct the groups and the sensitive-area list if they are wrong | **You** |
| 2 | Write the prompt (version 1) in this file, in the shape of `tech-debt.md`, "The prompt" | **Claude** |
| 3 | Create the `ready-for-agent` and `feature-dev` labels | **Claude**, when asked |
| 4 | Create the Routine, paste the prompt, compare the live text with this file, record the trigger ID | **You**; Claude can do the comparison |
| 5 | Add the ledger prefix to `operations/ledger/README.md` and the Routine to `CLAUDE.md`'s list | **Claude**, in the PR that deploys it |
| 6 | Read the first four weeks of rankings before deciding on version 2 | **You** |
