# The autonomy ladder

What an agent may decide on its own, per cycle, and what it takes to move up.

The point of this project's operating system is moving work from "asks the
maintainer" to "just does it." That move needs a rule rather than a mood —
otherwise it happens too early after a good week, or never, because there is
never an obviously right moment.

## The ladder

Rungs are defined once, by **blast radius** — what an action can reach — not
per cycle. A new cycle points at a rung; it does not get its own ladder. That
is what keeps the tenth cycle as cheap to add as the second.

| Rung | May | Cannot reach |
|---|---|---|
| **R0** | Read and report. Output is text to the maintainer. | anything |
| **R1** | Write to the repo's non-shipping surface — open a draft PR, write docs, file an issue | production, members |
| **R2** | Act on its own work in flight — push fixes to its open PR, answer review, re-run CI | merge |
| **R3** | Merge paths that cannot deploy — `docs/`, `operations/` | Cloud Run |
| **R4** | Merge shipping code — `app/`, `terraform/` | — |
| **R5** | Act on production directly, or message a member unsupervised | — |

## Registry

| Cycle | Rung | Clean streak | Promotion rule |
|---|---|---|---|
| morning post-mortem | R0 | see ledger | 5 clean runs at R0 → R1, docs paths only |
| ship-pr | R2 | see ledger | capped at R2 until R3's scorer exists |
| PR check-ins | R2 | see ledger | capped at R2 |

Streaks are **computed, never hand-maintained** — count back through
`operations/ledger/runs.jsonl` for that cycle until the first `clean: false`. A
counter maintained by hand rots the first busy week.

### Where the post-mortem cycle actually stands

It has run daily since 2026-08-20 and produced 19 post-mortem documents. The
maintainer has approved the documentation PR every time. On the evidence it has
earned R1 for docs-only paths.

It also has a recorded reset: on 2026-09-15 it reported the morning as having
no booking when two bookings had in fact won, and only a human pushback caught
it. The cause was a stale log query, fixed in #206. That reset is the rule
working — promotion should have been blocked until #206 landed, and a number
would have said so without anyone having to remember.

## What "clean" means

**A run is clean when its output needed no correction before it could be
used.** Not when the news was good.

A correctly diagnosed loss is a clean run. A confidently wrong diagnosis of a
win is not. The metric is about whether the cycle can be trusted, not about
whether the morning went well.

## Who judges

This is the constraint that actually caps the ladder, so it is worth stating
plainly: **a cycle cannot be promoted past the point where something automated
can score it.**

At R0–R1 the maintainer judges, and that is free — they read the output anyway.
The whole point of R3 and R4 is that the cycle acts *without* being read. If
the maintainer is still judging every run there, the promotion bought nothing:
the label moved and the work did not.

| Rung | What scores a run | Have it? |
|---|---|---|
| R0–R1 | the maintainer | yes, free |
| R2 | CI green/red | **yes** — which is why `ship-pr` already works here |
| R3 | almost nothing | acceptable anyway: nothing deploys, damage ceiling is nil |
| R4 | CI must catch a bad deploy before members do | **partially** |

R3 is the instructive exception. A weak judge is fine there *because the blast
radius is nil*, not because the judge is good. Do not generalise from it.

### The R4 scorer

Two of the three known holes are closed as of #217:

- ~~mypy advisory (`continue-on-error: true`)~~ — now blocking (#158)
- ~~browser tests skip silently and stay green~~ — now fail under CI (#159)
- **no `terraform validate` in CI** — still open. A bad terraform change still
  reaches deploy unchecked.

Until that third one is closed, R4 would be scored by something that cannot see
infrastructure errors, and a streak counter would report "clean" on runs
nothing checked.

This is also why those issues are not ordinary test hygiene. They are
load-bearing parts of the operating system, and should be ranked as
infrastructure rather than by age or member impact.

## Demotion

The ladder goes both ways. A cycle at R3 that causes a member-visible defect
drops to R2; it does not merely reset its streak to zero.

Without demotion the only correction available is switching the whole thing
off, which is not a correction — it is a surrender.

Note that the defect is the **worst available judge**: by the time it fires, a
member has already been affected. It is the backstop for when the automated
scorer misses, not the plan.

## Adding a cycle

1. Write `operations/cycles/<name>.md` — what it does, when it fires, its
   prompt, and what "clean" means for this one specifically.
2. Start it at **R0**. No exceptions, including for cycles that look trivial.
3. Add a row to the registry above with a promotion rule.
4. Have it append a row to `operations/ledger/runs.jsonl` on every run.

Step 4 is not optional. A cycle that does not emit rows cannot have a streak,
which means it can never be promoted — it will sit at R0 forever regardless of
how well it performs.

## Member-facing work

R5 is the only rung that can reach a person, and it is the one to be slowest
about. Two notes for when it comes up:

**Intake and reply are separate cycles.** Capturing a member's bug report or
feature request into an issue is repo-internal — that is **R1**, and buildable
long before anything replies to anyone. If capture and reply are one cycle, the
whole thing gates at the highest rung and the useful half never ships.

**The repo is public.** Filing a member's message verbatim as a GitHub issue
publishes their words and habits to the open internet. Decide how identifiers
are handled *before* intake is built — a leaked issue can be deleted but not
un-indexed.
