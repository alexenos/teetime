# Routine: tech debt

**Status: proposed. Not deployed.** This file is the specification and the source
of the prompt. Deploying it is the steps under "Deploying it", none of which has
happened.

| | |
|---|---|
| **Trigger** | weekly, Saturday, `0 9 * * 6` UTC (04:00 CDT, 03:00 CST), proposed |
| **Runs as** | a new Claude Code session per firing, with no prior context |
| **Model** | `claude-sonnet-5-5`, proposed |
| **Authorization** | reads the repository and GitHub. Creates and edits issues and labels. Pushes one branch and opens one PR, triggers CodeRabbit on it and answers its findings. Writes one ledger object to GCS. **Never merges.** |
| **Emits** | GitHub issues (the findings), at most one PR per run, and one ledger object per run at `operations/tech-debt/<YYYY-MM-DD>.json` in GCS |
| **Owns** | the tech-debt backlog (as GitHub issues) and its own run metadata |

## What it does

Three jobs in one run, in order.

1. **Scan.** Look for new debt in the categories below and file one GitHub issue
   per finding that is not already filed.
2. **Fix.** Look at every open tech-debt issue, not only the ones the scan just
   filed, choose one, and open one PR that fixes it.
3. **Review.** Trigger CodeRabbit on that PR and work through what it says, so the
   PR reaches the maintainer already reviewed. The maintainer then reviews and
   merges. The Routine does neither of those last two.

## GitHub is the source of truth

Every finding is an issue. The ledger holds run metadata only: did it run, what
did it cover, how many issues, which PR. It never holds a finding, and nothing
reads findings from it. If the two ever disagree about the backlog, the issues are
right.

That also fixes the dedup problem. The Routine has no memory between runs, so the
only way to know a finding is already filed is to ask GitHub.

**A fingerprint on every issue.** The body ends with an HTML comment
`<!-- td-id: <category>:<path>:<identifier> -->`, where the identifier is a
function, class, test name, variable name or rule code, not a line number, which
moves. Before filing, the scan reads the fingerprints out of the bodies of **all**
`tech-debt` issues, open and closed, and compares locally. It does not rely on
GitHub's issue search finding text inside an HTML comment, which is not verified.
Open: skip. Closed `wontfix`: skip, because the maintainer already
decided. Closed completed: file again only if the finding is still present, and
link the earlier issue.

**Labels.** `tech-debt` on every issue and every fix PR. One category label each:
`td:lint-suppression`, `td:todo`, `td:test-health`, `td:dead-code`,
`td:docs-drift`, `td:duplication`, `td:legacy-feature`, `td:code-quality`.
`td:blocked` for an issue the fix step should skip until the maintainer removes
it. `td:digest` for the one standing digest issue (below), which carries
`td:digest` and **not** `tech-debt`, so it is never in the dedup set or the fix
pool. The existing `wontfix` label means "decided
against".

## Categories

The signal comes from tools and from checkable facts, not from the model's taste.
A finding must carry the command or comparison that produced it.

| Category | Source |
|---|---|
| `td:lint-suppression` | `noqa` and `type: ignore` in `app/` and `tests/`, each with its rule code and whether the suppression is still needed (remove it, run ruff and mypy, see if anything fires) |
| `td:todo` | `TODO`, `FIXME`, `XXX`, `HACK` comments, cross-referenced against open issues; one that cites a closed issue is stale |
| `td:test-health` | skipped, xfail or conditionally skipped tests; tests with sleeps or time dependence; tests that touch real network or Chrome without the existing gating |
| `td:dead-code` | unused functions, classes and imports (vulture, or a grep that shows no references), terraform variables never referenced, config flags with no reader |
| `td:docs-drift` | a claim in `CLAUDE.md`, `operations/` or `.claude/skills/` that the code or the repository contradicts: a Routine marked "not deployed" that has a trigger ID, a path or function that no longer exists, a count that no longer matches |
| `td:duplication` | logic copied between modules, above all between the racer and the observer, which the maintainer has already said must share the booking path rather than fork it |
| `td:legacy-feature` | code that exists for a feature that is old, replaced, or never finished. **Judged against the feature manifest, not guessed**; see below |
| `td:code-quality` | code that is hard to read or wasteful, found by a measurable signal: ruff complexity and length rules, deep nesting, repeated I/O in a loop, a function whose docstring and body disagree. **Lowest priority.** At most 3 filed per run |

`td:code-quality` is the one category where the signal is partly judgment, so it
is fenced: a finding must name the measurable thing (a complexity score, a nesting
depth, a loop that repeats a call) and the concrete simpler form, and the fix step
takes one only when nothing else is available. "This could be designed better"
without either is not a finding.

### Legacy features need a manifest

Whether code is abandoned is the maintainer's call, and code alone does not
establish it. A function with no callers may be an entry point nobody grep finds;
a provider that is switched off may be about to be switched on. So this category
runs against `operations/features.md`, a list the maintainer owns, one row per
feature: name, status, the paths that implement it, and the date the status was
decided.

| Status | Meaning to the Routine |
|---|---|
| `live` | in use; never a finding |
| `experimental` | in progress; never a finding |
| `deprecated` | being retired; files an issue if its paths still exist, no fix |
| `removable` | maintainer has decided it can go; **eligible for a removal PR** |
| `planned` | not built yet; code for it is a question, not a finding |

The Routine files an issue when:

- a manifest path no longer exists (the manifest is stale, a `td:docs-drift` finding)
- a `deprecated` or `removable` feature still has code
- code under `app/` belongs to no manifest feature, **as a request to classify it,
  never as a claim that it is dead**. "No callers found" is stated as a hypothesis
  with the search that produced it

It never decides on its own that a feature is abandoned and never removes
anything the manifest does not mark `removable`. The manifest does not exist yet.
Until it does, the category records `ran: false, reason: "no_manifest"` and is
skipped. That is **not** a failed check and does not make the run `ok: false`.

Candidates the maintainer may want to classify first, from the file names alone.
These are hypotheses, not findings: the Discord provider and gateway, the Twilio
and SMS paths, the Gemini service, and the browser-driven `walden_provider`
alongside the HTTP booker.

### Finding nothing is a valid outcome

A run may file no issues, may fix nothing, or both. Each is `ok: true` when the
checks ran and covered files:

- scanned honestly, found nothing new: `ok: true`
- scanned, nothing new, but open issues exist: it still fixes one
- scanned, nothing new, no open issues to fix: `ok: true`, `fix.skipped:
  "no_candidate"`, a short digest comment saying so, a notification, and nothing
  else. It does not invent work, lower its bar, or open a PR to have something to
  show.

### A scan that finds nothing must be distinguishable from one that did not run

This repository has misdiagnosed three times from a valid zero-row result. So the
ledger row records, per category, whether the check ran, how many files it
covered, and how many findings it produced before dedup. `0` with
`files_covered: 0` is a failed check, and the run is `ok: false`.

## Choosing what to fix

The Routine considers all open issues labelled `tech-debt`, excluding `wontfix`,
`td:blocked`, and any that already has an open fix PR. It picks one and states why
in a comment on the issue before touching code.

**First version of the rule, to be refined:**

1. Prefer a finding that is small, self-contained and verifiable by the existing
   test suite, ruff and mypy.
2. Among those, oldest first, so the backlog does not only grow.
3. `td:code-quality` goes last: take one only when nothing else is eligible.
   `td:legacy-feature` is eligible only for a `removable` feature.
4. **Deprioritize, but do not forbid,** anything in the sensitive areas below.
   Skip one only if an alternative exists; if nothing else is open, it may be
   chosen, and the PR says so.

The maintainer has said these should not be fixed soon, and nothing is formally
off-limits:

- `app/racer/`, `app/observer/`, and the burst, gate and clock timing code
- the booking path in `app/providers/` (`walden_http_booker.py` and its callers)
- `terraform/`, `cloudbuild.yaml`
- credential, KMS and encryption code
- anything the 06:28 CT race executes

If every open issue is blocked, capped, or already has a PR, the run fixes nothing
and says why in the ledger.

## The PR cap

At most **3** open PRs from this Routine at any time, counted as open PRs labelled
`tech-debt`. At or over the cap the run still scans and files issues, picks
nothing, and records `fix.skipped: "cap"` with the count. The cap exists so that a
backed-up maintainer is not buried; it is a count, not a quality judgment.

## Authorization

| Can | Cannot |
|---|---|
| read the repository, GitHub, and the ledgers | merge any PR, by any route, including its own |
| create and edit issues and labels, comment on issues, and keep the one digest issue | close an issue itself, or reopen one the maintainer closed `wontfix`. A fix PR's `Closes #<n>` closes the issue only when the maintainer merges it. |
| push one branch and open one non-draft PR per run, and push further commits to that branch in response to review | push to `main`, or to a branch that is not its own |
| trigger CodeRabbit on its own PR, and reply to its findings there | trigger or reply on any other PR |
| write one object to the `operations/tech-debt/` prefix in GCS | edit another Routine's file or ledger |

This is not a standing authorization to write to `main`. CLAUDE.md names two
exceptions and this is not a third. A fix PR touching a path outside the Cloud
Build `ignored_files` list rebuilds and redeploys the service when the maintainer
merges it, and that is the maintainer's decision. The list is read from
`terraform/main.tf` on each run, not copied here, because it changes: #280 added
`tests/**` and `scripts/**` on 2026-10-09 (applied 03:03Z), so a tests-only or
scripts-only merge no longer deploys. A filter entry is live only once its own
merge has applied; the build log shows it (CLAUDE.md, "Verify an apply"). A merge landing close to the 06:28 CT job is
worth avoiding; the PR says when it would be safe.

The Routine also reads `MEMBER_PSEUDONYM_LABELS` from Secret Manager, for the name
check below.

## CodeRabbit review

CodeRabbit never reviews this repository on its own (a star threshold and a draft
rule both suppress it), so the Routine asks. The mechanics are in
`.claude/skills/ship-pr/SKILL.md` and this section does not restate them; the
prompt tells the session to read that file and follow its trigger, rate-limit and
judgment sections. What this Routine adds is a budget, because it runs unattended.

**The budget.** The quota is one included review per hour, shared across every PR
in the repository, and the stated wait has not always held (skill, "The named wait
is a lower bound"). A session cannot sit for an hour, so:

- **Round 1** is always attempted: trigger on the opening commit, name the short
  sha, wait for the acknowledgement to stop changing (30 seconds, per the skill),
  then poll for findings for up to 20 minutes. Landed reviews have taken 6 to 9
  minutes.
- **If refused,** read the wait it names. Schedule one retry at that time plus
  margin if the session can (`send_later`, which the skill says exists on the web
  and not in the desktop app; **whether a Routine session has it is not
  established**). If it cannot, or the retry is also refused, stop. The PR is
  opened and unreviewed, says so at the top, and the notification says so. Do not
  loop and do not post repeated triggers.
- **Round 2** (re-review of the fixes pushed after round 1) is attempted only if a
  fresh quota hour is reachable by the same scheduled wake-up. Otherwise the fixes
  stay un-reviewed, and the PR states exactly which commits CodeRabbit has seen
  and which it has not. At most two rounds in a run.

**Resolving findings.** Follow the skill's judgment section: read the reasoning and
ignore the severity label, check each finding against the code before agreeing or
disagreeing, and run any suggested diff before committing it, since its proposals
are plausible and environment-blind. Then, per finding:

| Judgment | Action |
|---|---|
| Real, and inside this issue's scope | fix it, with the test that would have caught it, in a new commit |
| Real, outside this issue's scope | do not grow the PR: reply, and file it as a new `tech-debt` issue (name check first), which is where the backlog lives |
| Wrong | reply with the evidence from the code, plainly |
| Style, matches surrounding code | apply it |
| Style, does not match | decline, and say why |
| Would change behavior | do not apply it here: reply, file an issue |

Reply on the finding's own thread (the parent comment, not a reply's id), grouped
into one comment per round where findings are minor, and run the full pre-push
command before each push. The Routine does not mark threads resolved on GitHub; a
reply stating what was done is the record, and the maintainer closes them.

**The Routine does not treat a clean review as approval, and neither should the
maintainer.** Per the repo's own history, a green CodeRabbit check proves nothing,
and its walkthrough and "Merge Risk" are stamped with the commit they covered and
go stale after any later push. The PR body states the sha the last review covered.

## Every issue and every PR carries risk and verification

The maintainer reviews the PR, so the PR has to make review cheap and honest.

**Risk, stated at the top of the PR body:**

- **Area:** which files, and whether any is in a sensitive area above.
- **What merging does:** compare the changed paths against the Cloud Build
  `ignored_files` list in `terraform/main.tf`, read fresh. Say plainly whether the
  merge redeploys the live service and applies terraform, or does neither. The
  filter skips the build only when every changed file matches, so one file outside
  the list is enough to deploy. A commit that touches `terraform/` always builds.
- **Race exposure:** whether anything the 06:28 CT job executes changed, and what
  the maintainer should watch on the next race morning if so. If the merge
  redeploys, say that it should not land between 06:15 and 06:45 CT.
- **Behavior:** the PR is meant to preserve behavior. If it does not, the PR says
  how, and that belongs in the title.

**A Review section** records what CodeRabbit covered: the short sha it reviewed,
the sha of the head, whether those differ, each finding with its disposition
(fixed, filed as an issue, declined with the reason), and anything it never
reviewed. A review that was refused, or never landed, is stated at the top of the
PR, not omitted.

**Verified and not verified, in separate sections**, as CLAUDE.md requires. The
pre-push command is run in full and its result is quoted, not summarized:

```bash
poetry run pytest -q && poetry run ruff check . && \
  poetry run ruff format --check . && poetry run mypy app
```

What a green run does not establish is the more important half. A test suite that
cannot exercise booking says nothing about booking: this project has no local way
to exercise it (testing means deploying). The PR names what the tests do and do
not cover for the changed code, and what remains a hypothesis.

## The digest: the list to review

Reviewing the week should not mean opening every issue. The Routine keeps **one
standing issue** labelled `td:digest` (created on the first run, titled
`Tech debt digest`) and adds **one comment per run**. Subscribing to that issue
is the review queue. The digest is a pointer; the findings themselves stay in
their own issues, which remain the source of truth.

The comment lists every issue the run created or found, each as the title
linked to the issue, then one sentence saying what it is:

```markdown
Tech debt scan 2026-10-10

New this week (2)
- [Remove unused noqa on parse_slots](https://github.com/OWNER/REPO/issues/1) - The suppression no longer silences anything, so it can be deleted.
- [Skipped test for the retired SMS path](https://github.com/OWNER/REPO/issues/2) - A test has been skipped since the SMS flow was replaced and asserts nothing.

Found again, still open (1)
- [Duplicate login retry in racer and observer](https://github.com/OWNER/REPO/issues/3) - The same retry loop is copied in two modules.

Fix this week: [PR](https://github.com/OWNER/REPO/pull/4) for issue 3, risk low, merge does not redeploy.
```

The URLs above are placeholders. Rules: a sentence is a sentence, not a
paragraph; one line per issue; "found again" means the scan re-detected a finding
whose issue is already open; a week with nothing says `Nothing new, nothing
open to fix.` rather than posting nothing, because a missing comment would be
indistinguishable from a missed run. The comment passes the name check like
everything else public.

## The member-name check

Every issue, issue comment, PR body, PR title, review reply, commit message and
branch name, and every later edit to any of them, is public. The repository is public, and the scan reads code, tests and fixtures that
may contain names, phone numbers, Telegram handles or member numbers.

**Before any of those is created**, the text is written to a file and checked,
exactly as the race report does (`.claude/skills/race-report/SKILL.md` §8a):

```bash
install -d -m 700 ~/.teetime && (umask 077; gcloud secrets versions access latest \
  --secret=MEMBER_PSEUDONYM_LABELS --project=gen-lang-client-0822973627 > ~/.teetime/labels.json)
poetry run python scripts/check_report_names.py <file> --labels ~/.teetime/labels.json
```

Exit 0 is the only pass. Exit 1: replace the named lines with labels and recheck.
Exit 2: it could not check, which is not a pass. Do not create the issue or PR; the
run is `ok: false` with a note. Delete `~/.teetime/labels.json` afterwards.

**A finding that is itself a name.** If the scan finds an identifier committed to
the repository (a name in a fixture, a handle in a doc), the issue gives the file,
the line and the kind of identifier, **never the string**, and the check runs over
the issue text to confirm. That finding is exactly as sensitive as the thing it
reports, and is a violation of CLAUDE.md in its own right. Say so in the issue.

**What the check does not cover, not verified:** it matches the names it is given.
Run without `--artifacts` here, because this Routine has no morning's tee sheets
to read, it knows only the labels file. A name not in the labels file passes it.
Whether the tee-sheet fixtures under `tests/fixtures` should also be passed is not
established; check how `--artifacts` reads a directory before relying on it.

## What a run does

1. **Set up.** `TZ=America/Chicago date '+%F %H:%M'` for the date, then
   `bash scripts/setup_remote_env.sh`. `READY` is the only pass. On anything else,
   write a ledger row with `ok: false` and the failed path, notify, stop.
2. **Read the backlog.** List every issue labelled `tech-debt`, open and closed,
   with bodies. This is the dedup set and the fix pool. There is no date window:
   a `wontfix` closed long ago must still suppress a refile.
3. **Count the Routine's PRs.** List PRs labelled `tech-debt` and record how many
   are merged, closed unmerged, or open, cumulatively. The Routine has no memory of
   the last run, so a reader diffs consecutive rows for the week's change. This is
   the quality measure and costs one list call.
4. **Scan.** Run each category's checks. File an issue per new finding, after the
   name check. Record per-category coverage.
5. **Fix.** Apply the cap. If clear, choose an issue by the rule above, comment
   the choice, create a branch `tech-debt/<issue number>-<slug>`, make the change,
   run the pre-push command, run the name check over the PR text and commit
   message, push, open the PR with `Closes #<n>` and the risk and verification
   sections. Label it `tech-debt`.
6. **Review.** Trigger CodeRabbit on the PR, within the budget above. Judge and
   answer each finding, push fixes, and re-trigger if quota allows. Update the PR
   body's Review section to match the final state.
7. **Digest.** Post the run's comment on the digest issue: new, found again, and
   the fix PR with its risk line.
8. **Ledger row, then notify.** In that order, on every path.

## The ledger row

One object per run, never an edit, per `operations/ledger/README.md`:

```
gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/tech-debt/<YYYY-MM-DD>.json
```

The values below are invented to show the shape; the issue and PR numbers are
not real runs.

```json
{"date":"2026-10-10","routine":"tech-debt","ok":true,
 "scan":{
   "categories":{
     "lint-suppression":{"ran":true,"files_covered":84,"found":6,"filed":2},
     "todo":{"ran":true,"files_covered":84,"found":9,"filed":1},
     "legacy-feature":{"ran":false,"reason":"no_manifest"}},
   "filed_total":3,"open_backlog":14,
   "digest_comment":"<url>"},
 "fix":{"issue":301,"pr":302,"skipped":null,
        "sensitive_area":false,"open_prs_at_start":1},
 "review":{"requested":true,"rounds":1,"outcome":"reviewed",
           "findings":4,"fixed":2,"filed":1,"declined":1,
           "head_reviewed":true},
 "prior_prs":{"merged":1,"closed_unmerged":0,"open":1}}
```

`ok` describes the run. A run that scanned honestly and found nothing is
`ok: true`, and so is a run that fixed nothing because nothing was eligible. A
category with `ran: false` and `reason: "no_manifest"` is skipped, not failed. A
run whose check covered zero files, whose name check could not run,
or whose PR failed its own pre-push command and was opened anyway is `ok: false`
with a note. `fix.skipped` is `null` when a fix was opened, otherwise one of
`cap`, `no_candidate`, `check_failed`, `error`.

`review.outcome` is one of `reviewed`, `rate_limited`, `no_response` or `not_run`
(no PR was opened). `head_reviewed` is false whenever fixes were pushed that
CodeRabbit never saw. Counts are of findings, from the review comments, not from
the severity labels. A run that opens a PR CodeRabbit never reviewed is still
`ok: true` if it said so; one that implies a review that did not happen is
`ok: false`.

**The measure that matters is `prior_prs.closed_unmerged` against `merged`**, both
cumulative. If the maintainer closes more than about half of what this Routine opens, the categories
or the selection rule are wrong, and that is the signal to change them. It is
sampled once a week, so it is slow; that is acceptable at this volume.

The schema belongs in `operations/ledger/README.md` beside the others, with a
`operations/tech-debt/` row in its prefix table. Add it when this is deployed.

## Notification

Notify the maintainer every run, since a weekly cadence makes silence ambiguous.
One or two sentences, no markdown, labels not names. For example:

- `Tech debt - 3 new issues filed, 14 open. Fix PR #302 opened for #301, CodeRabbit reviewed it and 2 of 4 findings were fixed, touches docs only, merge does not redeploy.`
- `Tech debt - 2 new issues filed. No fix: 3 PRs already open.`
- `Tech debt - fix PR #302 opened, touches app code, merge redeploys, CodeRabbit rate limited so not reviewed.`

## Open items

- **DST.** `0 9 * * 6` is 04:00 CDT and 03:00 CST from 2026-11-01. Cosmetic: 03:00
  is still hours before the 06:28 race and nothing here races a clock. It joins the
  2026-11-01 review as "leave it", unless 04:00 is wanted year-round, in which case
  it moves to `0 10 * * 6` that day.
- **Saturday 04:00 CT** is well clear of the 06:28 CT job, so a quota hour should
  be free. The Routine runs in a cloud session, not in the Cloud Run service, so it
  cannot compete for the single Chrome. Neither the quota nor a Saturday race has
  been checked.
- **Can a Routine session schedule a wake-up?** The review budget leans on
  `send_later` for any retry or second round. If it cannot, the Routine does one
  review round per week and the PR says what was left unreviewed. Check on the
  first run.
- **A refusal at 04:00** costs that PR its review unless the wake-up exists. The
  maintainer can always run `ship-pr` on the open PR.
- **Selection is a first guess.** Oldest-first among small issues has no evidence
  behind it yet. Expect to rewrite it after a few weeks of closed-unmerged data.
- **The category checks are unrun.** None of the commands above has been run
  against this repository for this purpose. Number of existing suppressions, TODOs
  and skipped tests is not established, so the first scan may file a large batch.
  The prompt therefore caps filing at 15 issues per run, which is a first guess
  chosen for that reason; the remainder is counted in the ledger note and filed on
  later runs.
- **Label creation.** The labels do not exist yet. The Routine creates any that are
  missing at the start of each run (Step 1 of the prompt), so no manual step is
  needed, but it makes the first run's GitHub write access the first thing tested.

## The prompt

```
You are running the weekly tech-debt Routine for the TeeTime booking bot. You start with zero context; everything you need is in this repo. Read operations/routines/tech-debt.md first: it is the specification, and it governs where this prompt is silent.

You find tech debt, record each finding as a GitHub issue, and fix one open issue per week by opening a pull request. You never merge anything, ever. The maintainer reviews and merges. You are not authorized to push to main, to merge your own PR, or to close an issue you did not file.

GitHub is the source of truth for findings. The ledger is run metadata only.

Rule that applies to every path through this prompt: always write your ledger row, then always send a push notification, in that order. Whatever happens, finish by writing one object to gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/tech-debt/ named for the run date in Central Time, never overwriting, and then calling PushNotification. Every stop in this prompt means ledger row, push, stop. If the ledger write fails, retry once and then say so in the notification.

Rule that applies to every path through this prompt: no names. Issues, comments, PR titles and bodies, commit messages and branch names are public. Before creating or editing any of them, write the text to a file and run scripts/check_report_names.py against it with --labels ~/.teetime/labels.json, reading the labels from MEMBER_PSEUDONYM_LABELS as .claude/skills/race-report/SKILL.md section 8a describes. Exit 0 is the only pass. Exit 1 means replace the named lines with labels and recheck. Exit 2 means it could not check; that is not a pass, so do not create it, record ok false with a note, and say so in the notification. If a finding is itself an identifier committed to the repo, give the file, line and kind of identifier and never the string.

Step 1 - set up. Get today's date with TZ=America/Chicago date. Run bash scripts/setup_remote_env.sh. Only READY is a pass. On anything else, write the ledger row with ok false naming the failed path, notify, and stop. Then make sure these labels exist and create any that are missing: tech-debt, td:blocked, td:digest, td:lint-suppression, td:todo, td:test-health, td:dead-code, td:docs-drift, td:duplication, td:legacy-feature, td:code-quality. If you cannot create a label, that is a failed run: record ok false, notify, and stop.

Step 2 - read the backlog. List every issue labelled tech-debt, open and closed, with bodies, and extract the fingerprint from each body. Use no date window: a wontfix closed long ago must still suppress a refile. This is your dedup set and your fix pool. Then list PRs labelled tech-debt and count, cumulatively, how many are merged, closed without merging, or open.

Step 3 - scan. For each category in the spec, run its checks and record how many files each covered and how many findings it produced. A check that covered zero files failed; say so and mark the run ok false. For each finding compute its fingerprint, category:path:identifier with no line number, and compare it with the fingerprints from Step 2. Skip it if the matching issue is open, or closed wontfix. If the matching issue is closed as completed and the finding is still present, file a new issue that links the old one. Otherwise file one issue, after the name check, with the tech-debt label and one td: category label. The body must give the location, the command or comparison that produced the finding, why it is debt, a suggested fix, which sensitive area it touches if any, what you verified and what you did not, and end with the fingerprint as an HTML comment. For td:legacy-feature, read operations/features.md. If it does not exist, record ran false with reason no_manifest and skip the category; that is not a failure. Never decide a feature is abandoned yourself: classify against the manifest, and state any no-callers observation as a hypothesis with the search that produced it. For td:code-quality, name the measurable signal and the concrete simpler form, and file at most 3 per run. Do not file more than 15 issues in one run; if there are more, file the 15 most clearly supported and say how many remain in the ledger note. Filing nothing is a valid outcome: do not invent findings to have something to report.

Step 4 - fix, if clear. Count open PRs labelled tech-debt. If there are 3 or more, fix nothing and record skipped as cap. Otherwise consider every open tech-debt issue except those labelled wontfix or td:blocked or that already have an open PR. Prefer small, self-contained issues the existing tests, ruff and mypy can verify; among those, oldest first. Take a td:code-quality issue only if nothing else is eligible, and a td:legacy-feature issue only if the manifest marks the feature removable. If nothing is eligible, fix nothing and record skipped as no_candidate; do not open a PR to have something to show. Deprioritize issues in the sensitive areas the spec lists, but do not refuse them if nothing else is open, and say so in the PR. Comment on the chosen issue saying why you chose it. Make a branch named tech-debt/<issue number>-<short slug>, preserve behavior, and make the change.

Run the full pre-push command and quote its result: poetry run pytest -q, poetry run ruff check ., poetry run ruff format --check ., poetry run mypy app, all of them. If any fails, do not open the PR; record skipped as check_failed.

Open a normal PR into main with the tech-debt label and Closes #<n>. The first section of the body is Risk, stating: the files and whether any is a sensitive area; whether merging redeploys the service or applies terraform, determined by comparing the changed paths with the Cloud Build ignored_files list, read fresh from terraform/main.tf rather than recalled, where a change whose every file matches the list does neither and any file outside it does both; whether anything the 06:28 CT race executes changed and what to watch on the next race morning; and whether behavior changed. Then a Review section (filled in at Step 4b), then Verified and Not verified, kept separate. Say what the tests do and do not cover for the changed code. The project has no local way to exercise booking, so do not imply a green suite establishes booking behavior.

Step 4b - CodeRabbit. Read .claude/skills/ship-pr/SKILL.md and follow its sections on triggering, rate limits, reading the review and judging each comment. CodeRabbit never reviews this repo on its own, so post a comment on your PR reading @coderabbitai review - new commit <short sha>, with the sha of the head. Wait until its reply has stopped changing for 30 seconds, because the first wording can be edited into a refusal, then poll for findings for up to 20 minutes. Fetch both the inline review comments and the issue comments, and take only top-level comments.

If it refuses, read the wait it names. If you can schedule a wake-up, schedule one retry at that time plus two minutes. If you cannot, or the retry is also refused, stop trying: leave the PR open, state at the top of the PR that CodeRabbit did not review it, record review outcome rate_limited, and go to Step 5. Never post a second trigger for a commit it has already seen, and never post a column of triggers.

If it reviews, judge each finding on its reasoning, not its severity label, and check it against the code before agreeing or disagreeing. Run a suggested diff before committing it. Fix a real finding that is inside this issue's scope in a new commit with the test that would have caught it. If a finding is real but outside this issue, or would change behavior, do not widen the PR: reply to it and file it as a new tech-debt issue. If a finding is wrong, reply with evidence from the code. Reply on each finding's own thread using the parent comment id. Run the name check over every reply before posting it. Do not mark threads resolved; your reply is the record.

Run the full pre-push command before every push. After pushing fixes, re-trigger once, naming the new sha, only if you can schedule a wake-up for a time after the quota hour has reset; this session just used that hour. Otherwise do not, and say in the PR exactly which commits CodeRabbit reviewed and which it did not. At most two rounds. Update the Review section of the PR body to the final state: the sha reviewed, the sha of the head, each finding and what you did with it. A clean review, or a green CodeRabbit check, is not approval and you must not describe it as one.

Step 5 - digest. Find the issue labelled td:digest, or create it, titled Tech debt digest, with only that label. Post one comment for this run: a heading with the date, then New this week and Found again, still open, each issue on one line as the title linked to the issue followed by a dash and one sentence saying what it is, then one line for the fix PR with its risk, or why there is none. If there is nothing, say Nothing new, nothing open to fix. Never post nothing. Run the name check over the comment first. Record the comment URL in the ledger.

Step 6 - ledger row, then notification. The schema is in the spec. ok describes the run: a run that scanned honestly and found nothing is ok true; a failed check, a name check that could not run, or any problem is ok false with a note. Then push a notification of one or two sentences, no markdown, using labels not names: how many issues were filed, how many are open, that the digest is posted, and either the fix PR number with whether merging redeploys and whether CodeRabbit reviewed it, or why nothing was fixed.
```

## Deploying it

Who does each step. **You** means a manual step that only the maintainer can do;
**merge** means it happens when this PR merges; **Claude** means a session can do
it when asked.

| # | Step | Who |
|---|---|---|
| 1 | Merge this PR. Touches only `operations/`, so no build and no deploy. | **You** |
| 2 | The ledger prefix row and schema in `operations/ledger/README.md`, and the empty `tech-debt.jsonl` | Done in this PR (**merge**) |
| 3 | Create the labels: `tech-debt`, `td:blocked`, `td:digest`, and the eight `td:` categories | **Automatic**: the Routine creates any missing label at the start of each run. Optional to do it yourself first |
| 4 | Write `operations/features.md` (the feature manifest). Without it `td:legacy-feature` is skipped, which is allowed, so this is not a blocker for deploying | **You** decide each status. Claude can draft the table from the code for you to correct |
| 5 | Confirm the Routine can write the ledger. `teetime-artifact-reader` holds `objectCreator` on the `operations/` prefix (`terraform/main.tf`, `routine_ledger_writer`), which covers `operations/tech-debt/`. **Not confirmed:** that the Routine runs as that account | **You**, in the Routine's environment |
| 6 | Confirm the Routine's account can read `MEMBER_PSEUDONYM_LABELS`. `terraform/` grants Secret Manager access to the Cloud Run account, and **nothing found grants it to `teetime-artifact-reader`**; the race report Routine reads the same secret, so it probably works, but this is unverified | **You** check; add a terraform grant if it fails |
| 7 | Confirm the Routine's GitHub access can create issues and labels, push branches and open PRs. The race report only needs to push and open PRs | **You**, in the Routine's repository and connector settings |
| 8 | Create the Routine in the routines UI: name, cron `0 9 * * 6`, model, repository, environment, and paste the prompt below | **You**. An agent cannot create it; the existing triggers say so |
| 9 | After pasting, compare the live text with this file (a pasted prompt is read as markdown and loses paired asterisks; this one has none) | **You** |
| 10 | Record the trigger ID in this file, change **Status**, and add the Routine to `CLAUDE.md`'s list | Claude, in a small PR once you have the ID. The commit touches only ignored paths |
| 11 | Optional: run the category checks once by hand to see how many findings the first scan files | Claude, on request |
| 12 | Watch the first run, Saturday 2026-10-10 at 04:00 CDT. It is the first test of: label use, the 15-issue cap, the digest, the name check, and whether a Routine session can schedule a wake-up for a CodeRabbit retry | **You** (Claude can review the result) |
