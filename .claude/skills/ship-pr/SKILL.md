---
name: ship-pr
description: Open a PR, wait for CodeRabbit and CI, then work through the review comments. Use when the user says to create a PR and handle the review, respond to review comments, address CodeRabbit feedback, or asks what the bot said about a PR.
---

# Ship a PR through review

This repo reviews every PR with **CodeRabbit** (a GitHub App, so it does not
appear in `.github/workflows/` — only `test.yml` does). Checks are `CodeRabbit`,
`lint`, and `test`.

**CodeRabbit never reviews this repo on its own. You have to ask, every time.**
Do not wait for a review that is not coming; do not re-derive this each session.
Two separate rules both suppress the automatic pass, and either is enough:

- **Drafts are skipped by default.** The bot says so itself: *"Draft PRs are not
  automatically reviewed by default."*
- **The repo is under the star threshold**, which suppresses automatic reviews
  even once the PR is out of draft: *"This repository does not receive automatic
  reviews because it has fewer than 10 stars."*

The second one is the surprise, and the reason this section exists: taking a PR
ready-for-review looks like it should start a review, and does not.

Both conditions are mutable, so treat them as observed rather than permanent -
as of 2026-09-19 the repo had 0 stars. Re-check rather than assume when the
behaviour differs:

```bash
gh api repos/alexenos/teetime --jq .stargazers_count   # against the threshold above
```

The bot states its own configuration in the summary comment it posts on every
PR, inside a "⚙️ Run configuration" block. Read it there rather than trusting
this file. *Historical:* on #211, #212 and #217 (2026-09) it said **Plan:
Advanced**, profile `ASSERTIVE`, configuration from the Organization UI. On #247
(2026-10-03) it said profile `CHILL`, configuration from the repository
`.coderabbit.yaml`, which is the current setup.

**The quota line is in the review body, not the summary comment.** An earlier
version of this skill said it did not appear at all; that was a search in the
wrong place. The review CodeRabbit submitted on #247 (2026-10-03T00:50:15Z,
commit `63d701b`) carries it inside its "Review info" details block, read from
`gh api repos/alexenos/teetime/pulls/<N>/reviews`:

```
Included review availability: This review used your included allowance. Your
plan provides up to 1 included review per hour; 0 remain after this review.
```

It reports the state *after* a review, so it tells you whether the next trigger
will be refused; it does not replace §Rate limits below.

So the trigger is a comment, posted by you, naming the commit you want looked at
(CodeRabbit is incremental and will not re-review commits it has already seen -
it says so itself: *"CodeRabbit is an incremental review system and does not
re-review already reviewed commits"*):

```bash
gh pr comment <N> --body "@coderabbitai review - new commit $(git rev-parse --short HEAD)"
```

Post it **when you open the PR**, and **again after every push** you want
reviewed, including a ready-for-review transition.

### No `gh`? Use the GitHub MCP tools

Claude Code on the web has no `gh`, `hub`, or GitHub API access - only the
`mcp__github__*` tools. Every `gh` command in this file has an equivalent, and
the automation below has to work on either. The ones this skill needs:

| `gh` | MCP |
|---|---|
| `gh pr create` | `mcp__github__create_pull_request` |
| `gh pr comment <N> --body ...` | `mcp__github__add_issue_comment` |
| `gh pr view <N> --json comments` | `mcp__github__pull_request_read` method `get_comments` |
| `gh api .../pulls/<N>/comments` | `mcp__github__pull_request_read` method `get_review_comments` |
| `gh api .../pulls/<N>/comments/<ID>/replies` | `mcp__github__add_reply_to_pull_request_comment` |
| `gh pr checks <N>` | `mcp__github__pull_request_read` method `get_check_runs` |
| `gh api repos/{o}/{r} --jq .stargazers_count` | `mcp__github__search_repositories`, below |

The star count needs care. `mcp__github__search_repositories` returns a result
*list*, so select the row whose `full_name` is `alexenos/teetime` rather than
taking the first. Its field name also depends on the output mode: the default
`minimal_output: true` names it `Stars`, and `minimal_output: false` returns the
full GitHub object with `stargazers_count`, matching the `gh` command above.
Pass `minimal_output: false` and read `stargazers_count`, so the two paths agree.

### Rate limits, and re-prompting without sitting there

Triggering is cheap but not free, and a trigger that is refused looks almost
exactly like one that is still working. Drive it as a loop with a real clock
rather than by feel.

**1. Post the trigger, then read the bot's reply.** CodeRabbit acknowledges
fast - 7 seconds on #211 (`01:53:02` → `01:53:09`), 9 seconds on #217
(`23:32:43` → `23:32:52`). The ack is an issue comment from `coderabbitai[bot]`,
and it comes in **two wordings that mean different things**:

```
Action performed
Review triggered.          ← work is starting; findings are minutes away
```

```
Action performed
Review finished.           ← nothing left to do on this commit
```

Both confirm the trigger registered. Only the second means the review is over -
it appears when every commit in range has already been reviewed, since
CodeRabbit is incremental. Read "Review triggered" as the start of a wait, not
the end of one; the summary comment then shows *"Currently processing new
changes in this PR"* while it runs, and findings arrive afterwards as inline
review comments ("## 4. Read the review properly"). On #217 that gap was about
six minutes.

**2. Classify the reply.** Three outcomes, and they need different waits:

- **Ack, as above** → triggered. Go to "## 3. Wait" below and poll for
  findings.
- **A reply naming a wait** → the common case when it refuses, and the one
  that needs no guessing. See step 3.
- **No reply at all within ~3 minutes** → **re-fetch the bot's comments
  before doing anything.** An ack can land between the check that found none and
  the repost, and a second trigger comment for the same head sha is public noise
  that buys nothing: CodeRabbit will not re-review a commit it has already seen,
  so the duplicate cannot even produce a second review. Only if the re-fetch
  still shows no ack, re-post once. If that attempt is also silent, treat it as
  rate-limited and use the fallback estimate in step 4.

**3. When it is rate limited, it tells you how long. Use its number.** The bot
states the remaining wait in the comment itself, so read the answer rather than
estimating around it. Take the most recent `coderabbitai[bot]` issue comment
after your trigger, pull the duration out of its text - it is written for
people, so expect a form like minutes and seconds rather than a machine field -
and convert it against that comment's own `created_at`, not against the clock
when you got round to reading it:

```
next_eligible = <created_at of the command reply> + <the wait it names> + 2 minutes
```

Anchoring on `created_at` matters because the comment may have been sitting
there for a while before this session looked; anchoring on "now" would wait out
the same window twice. The two minutes is boundary margin, for the same reason
step 4 uses 61 rather than 60.

**Observed wording, #217, 2026-09-19.** The refusal arrives in two places at
once. The command reply says:

```
⚠️ Action not completed
Review rate limited.
```

and the summary comment is edited to carry the number:

```
⚠️ Review limit reached
Next included review available in 48 minutes.

Limit details: You've used the included review currently available.
You've used all free OSS reviews for now. Wait for the free limit to reset to
keep reviewing this public repository.
```

**Anchor on the command reply, not the summary comment.** The wait appears in
both, but the summary comment is a single long-lived comment that CodeRabbit
edits in place all day - on #217 it was created at 04:53Z and carried the
rate-limit text after an edit at 23:44:55Z, nearly nineteen hours later. Using
its `created_at` would compute a retry time most of a day in the past. The
command reply is created fresh for each trigger, so its `created_at` is the
right anchor; if you can only read the summary comment, use its `updated_at`.

**Observed again, #247, 2026-10-02/03.** Trigger at 21:52:09Z; command reply
"Review rate limited." at 21:52:31Z; the summary comment was edited at
21:52:37Z to "Next included review available in 57 minutes." The retry at
00:43:49Z got "Review triggered." at 00:44:04Z, and the review landed at
00:50:15Z, about six minutes later. Established from the API on 2026-10-02: the
two trigger and reply timestamps, the "Review rate limited." text, and the
review's `submitted_at`. Not re-readable: the "57 minutes" text and the
"Review triggered." wording are as reported at the time, because both comments
have since been edited in place - the summary at 00:50:56Z, and the retry reply
at 00:51:01Z to read "Review finished." So a command reply's current text
can describe the review's end state rather than what it said when it was
posted. Read it soon after the trigger, or check `updated_at` against
`created_at`.

Note also that the refusal does not mean the trigger was lost - the walkthrough
still lists the commits it would have covered. Nothing was reviewed, so the
findings for that commit are still owed.

**4. Fallback, only when nothing states a wait.** Silence, or a refusal with no
duration in it. Anchor on a timestamp that exists rather than on when you
happened to ask:

```
next_eligible = <created_at of the last successful review ack> + 61 minutes
```

Both halves are readable from the API - the ack is an issue comment by
`coderabbitai[bot]` carrying `created_at`. If no successful review exists on
this PR yet, anchor on your own last trigger comment instead.

**61, not 60, and the extra minute is the point.** An estimate that lands
exactly on the boundary is refused as often as it succeeds - clock skew between
here and the bot, a window measured from a slightly later instant than the one
you anchored on, a retry that fires a few seconds early. Each near-miss costs a
whole cycle to discover, so buy the margin.

The hour is now **observed, not guessed**: the #247 review body (2026-10-03,
quoted near the top of this file) says the plan provides "up to 1 included review
per hour". Earlier versions of this skill called it a guess because a check of
#204, #211, #212 and #217 on 2026-09-19 found no rate-limit comment; #217 and
#247 have since produced two (step 3). What is still not established is how
the window is measured - rolling from the last review, or a fixed reset - so
keep the extra minute. Step 3 is still the real path when the bot names a wait.

**5. Back off, and stop.** Retry at `next_eligible`. If that attempt is also
refused, double the wait each time - 61 → 122 → 244 minutes - and **stop after
three refusals.** Tell the user what the bot said and that the review is not
coming on its own. Never spam the PR: each trigger is a public comment on the
thread, and a column of them is noise a reviewer has to scroll past.

**6. Schedule the retry; do not wait for it.** The re-prompt is a scheduled
wake-up, not a sleep. Use the `send_later` tool
(`mcp__Claude_Code_Remote__send_later`).

`next_eligible` from steps 3 and 4 is an **absolute timestamp**, and
`delay_minutes` is a **duration** - passing one as the other schedules the retry
at the wrong time. `send_later` takes an absolute time directly, so pass
`at: <next_eligible>` in RFC3339 and skip the arithmetic entirely. Use
`delay_minutes` only for a wait you computed as a duration to begin with, and
then from `now`, not from the anchor timestamp.

Carry in the message the PR number, the head sha you want reviewed, which
attempt this is, and what the bot last said. Then end the turn. A foreground
`sleep` burns the session for an hour and dies with the container; a scheduled
wake-up survives both.

**`send_later` is not available everywhere.** It exists in Claude Code on the
web; the desktop Code tab does not have it (observed 2026-10-02). There, a
background Bash wait works - `sleep <seconds>` with `run_in_background` - but it
is subject to the Bash background timeout and dies with the session, so it is a
weaker clock than a scheduled wake-up. Check that the wait fits inside the
timeout before relying on it.

Fold this into the PR check-in if one is already armed rather than running two
clocks against the same PR.

Its walkthrough, "Merge Risk", and pre-merge checks are each stamped with the
commit they covered - the summary says `up to <short-sha>`. After a later push
those are **stale**, not approval of the current head, so compare the sha they
name against `git rev-parse --short HEAD` before believing them.

Spend the review on a commit that is ready, not on a work in progress.

## 1. Branch and push

Never commit to `main` — a commit there redeploys to Cloud Run. Branch first.

```bash
git checkout -b fix/<short-slug> && git push -u origin fix/<short-slug>
```

## 2. Open it

`gh pr create` with a heredoc body. What makes these PRs reviewable:

- **Why before what.** Lead with the failure or observation that forced the
  change, not a summary of the diff.
- **State the accepted costs.** Regressions in latency, UX, or reliability that
  the change knowingly buys. A reviewer who finds one you didn't name assumes
  you missed it.
- **Flag what you could not verify** (e.g. terraform CLI is not installed
  locally, so `validate` never runs here).
- **Call out changed-not-added tests** and why the old assertion encoded the
  behaviour being replaced. This is the single most common source of review
  pushback.

## 3. Wait

Poll rather than sleeping in the foreground. Pass `run_in_background: true` to
the Bash tool — the loop itself is an ordinary foreground loop, the tool is what
detaches it and notifies you on exit.

The loop must **fail closed**. An auth error, a network blip, or an empty result
all produce output with no `pending` in it, and a naive check reads that as
"settled":

```bash
for i in $(seq 1 40); do
  if ! out=$(gh pr checks <N> 2>&1); then
    # A non-zero exit means either "still pending" (documented as 8) or a real
    # failure, so the output decides which. Verified the failure case: a bad PR
    # number exits 1 with "Could not resolve to a PullRequest" and no "pending",
    # which the old loop reported as a completed review.
    if ! grep -q "pending" <<<"$out"; then echo "gh failed:"; echo "$out"; exit 2; fi
  elif ! grep -q "pending" <<<"$out"; then
    echo "$out"; exit 0
  fi
  sleep 20
done
echo "still pending after 800s"; gh pr checks <N>; exit 1
```

CodeRabbit posts an initial summary comment within a minute of the PR opening —
that is **not** the review, and on this repo no review follows it unless you ask
(see the top of this file). Once triggered, the review lands later as inline
comments; the summary comment is edited in place as it goes, so its content
changing is not a review landing either.

Its `CodeRabbit` commit status also lags: it has sat on `pending — Review in
progress` after the review comment was already posted, and cleared only on a
later event. Trust the review text over the status badge, but do not call the
PR done until the status itself settles.

## 4. Read the review properly

Inline comments do not appear in `gh pr view --json comments`. That field only
holds issue-level comments. Fetch both, and **project `id`** — step 6 needs it
to reply, and re-fetching just to get it is pure friction:

```bash
gh api repos/{owner}/{repo}/pulls/<N>/comments --paginate \
  --jq '.[] | select(.in_reply_to_id == null) | {id, created_at, path, line, body}'
gh pr view <N> --json comments --jq '.comments[] | {author: .author.login, body}'
```

`select(.in_reply_to_id == null)` is not optional. The endpoint returns replies
alongside findings, so once you have answered a round the list is mostly your
own text, and the reply API needs the *parent* id — a reply's id will not
thread. Filtering also makes "did this push add findings?" answerable by
sorting the survivors on `created_at`.

Checks going green is not proof the inline comments have landed, especially on
a re-review after a push. If the comment list looks unchanged from before your
push, wait and re-fetch before concluding the review found nothing.

Three mechanics worth knowing:

- The single-comment route is `/pulls/comments/{id}`, **not**
  `/pulls/<N>/comments/{id}` — the latter 404s. Easiest is to fetch the list
  once and filter locally.
- CodeRabbit collapses most of each finding inside `<details>` blocks, so a
  one-line-per-comment summary shows only the severity tag. Read the whole
  body. "Nitpick" and "Outside diff range" sections are advisory.
- The bodies are emoji-heavy. When parsing with Python, set
  `PYTHONIOENCODING=utf-8` or strip to ASCII; this console is cp1252 and will
  raise `UnicodeEncodeError` on the severity emoji.

## 5. Judge each comment before acting

The reviewer is a tool, not an authority. For each finding, decide:

- **Real bug** → fix it, and add the test that would have caught it.
- **Real but out of scope** → say so, and spawn a follow-up rather than growing
  the PR.
- **Wrong** → say why, plainly and without hedging. A confident bot assertion
  that contradicts the code is still wrong. Verify against the code before
  agreeing *or* disagreeing — do not take a finding at face value, and do not
  reflexively defend the diff either.
- **Style preference** → apply it if it matches surrounding code, decline if it
  does not.

Do not "fix" something you believe is correct just to clear the review.

**Ignore the severity labels; read the reasoning.** They are unreliable in both
directions. On #145 a finding tagged `🔵 Trivial | 💤 Low value` pointed out that
a terraform `type = number` accepts `90.5`, which `tostring` emits as `"90.5"`
into a setting typed `int` — a container that will not boot, surfacing as a
failed deploy rather than a failed plan. Meanwhile some `🟠 Major` items were
documentation nits. Judge each one on what it says.

**Run the suggested fix before committing it.** CodeRabbit's proposed diffs are
plausible-looking and environment-blind. On #145 the suggested timezone handling
(`TZ=America/Chicago date -d ...`) is silently wrong here — Git Bash ignores
`TZ` when parsing and returns the input unchanged, so the "fix" would have
queried the wrong five hours and found nothing. Verify, then commit what you
verified, and leave a note saying why the obvious version was rejected.

## 6. Reply

Reply to each substantive finding. Inline replies thread properly:

```bash
gh api repos/{owner}/{repo}/pulls/<N>/comments/<COMMENT_ID>/replies -f body="..."
```

For a general reply: `gh pr comment <N> --body "..."`.

Group the response: what you fixed, what you're deferring and why, what you
think is wrong and why. One comment covering several findings beats a thread
per nit.

## 7. Push fixes and re-verify

Run the full local gate before pushing — CI is slower than you are:

```bash
poetry run pytest -q && poetry run ruff check . && poetry run ruff format --check . && poetry run mypy app
```

Ruff over `.`, not `app tests` — that is what both CI jobs run, and scoping
narrower locally lets a lint error in a file outside those two directories pass
here and fail there.

This gate matches CI. As of #217, `mypy app` is **blocking** in CI - the
`continue-on-error: true` flag that had been left behind after #10 fixed the
pre-existing errors is gone, so a type regression fails the build rather than
landing green. Run it locally anyway: CI is slower than you are.

Always `poetry run` — the local venv lives outside the repo, under Poetry's
cache. (CI is configured `virtualenvs-in-project`, so there it is `.venv`; both
need `poetry run` either way.)

Push, then **trigger the review again** and re-poll. CodeRabbit does not
re-review a push on its own here - the same two rules at the top of this file
suppress it every time, not just on the first round. Name the new head sha in
the trigger comment, and run the rate-limit loop again if it is refused.

## 8. Report, and stop

Summarize for the user: what the review caught that was real, what you pushed
back on, and what state the PR is in.

**Do not merge.** Merging to `main` deploys this service. That call is the
user's, always, unless they have explicitly said to merge in this session.
