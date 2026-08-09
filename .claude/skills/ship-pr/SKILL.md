---
name: ship-pr
description: Open a PR, wait for CodeRabbit and CI, then work through the review comments. Use when the user says to create a PR and handle the review, respond to review comments, address CodeRabbit feedback, or asks what the bot said about a PR.
---

# Ship a PR through review

This repo reviews every PR with **CodeRabbit** (a GitHub App, so it does not
appear in `.github/workflows/` — only `test.yml` does). Checks are `CodeRabbit`,
`lint`, and `test`.

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

Poll rather than sleeping in the foreground, and run it in the background:

```bash
for i in $(seq 1 40); do
  out=$(gh pr checks <N> 2>&1)
  if ! echo "$out" | grep -q "pending"; then echo "$out"; exit 0; fi
  sleep 20
done
```

CodeRabbit posts an initial summary comment within a minute of the PR opening —
that is **not** the review. The review lands later as inline comments.

## 4. Read the review properly

Inline comments do not appear in `gh pr view --json comments`. That field only
holds issue-level comments. Fetch both:

```bash
gh api repos/{owner}/{repo}/pulls/<N>/comments --jq '.[] | {path, line, body}'
gh pr view <N> --json comments --jq '.comments[] | {author: .author.login, body}'
```

CodeRabbit marks its findings with severity and often collapses detail inside
`<details>` blocks — read the whole body, not the first line. It also posts
"Nitpick" and "Outside diff range" sections that are advisory.

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
poetry run pytest -q && poetry run ruff check app tests && poetry run ruff format --check app tests && poetry run mypy app
```

Always `poetry run` — the venv lives outside the repo. Push, then re-poll;
CodeRabbit re-reviews each push.

## 8. Report, and stop

Summarize for the user: what the review caught that was real, what you pushed
back on, and what state the PR is in.

**Do not merge.** Merging to `main` deploys this service. That call is the
user's, always, unless they have explicitly said to merge in this session.
