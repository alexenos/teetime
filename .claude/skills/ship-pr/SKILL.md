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
that is **not** the review. The review lands later as inline comments.

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

This gate is deliberately **stricter than CI in one respect**: CI marks mypy
`continue-on-error: true` (pre-existing type errors), so a type regression will
not fail the build. Keep it fatal locally so new ones do not accumulate.

Always `poetry run` — the local venv lives outside the repo, under Poetry's
cache. (CI is configured `virtualenvs-in-project`, so there it is `.venv`; both
need `poetry run` either way.)

Push, then re-poll; CodeRabbit re-reviews each push.

## 8. Report, and stop

Summarize for the user: what the review caught that was real, what you pushed
back on, and what state the PR is in.

**Do not merge.** Merging to `main` deploys this service. That call is the
user's, always, unless they have explicitly said to merge in this session.
