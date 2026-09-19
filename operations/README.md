# operations/

How this project is run, as opposed to how it is built.

TeeTime books tee times for one retired golfer and his friends. It is also a
nine-month experiment in whether a working application can be developed
agentically, by someone who does not read the code. That experiment has an
operating rhythm now, and this directory is that rhythm written down so it can
be executed, measured and — where it has earned it — handed over.

Not in `docs/`: that directory is served publicly as GitHub Pages. This is
internal.

## What's here

| | |
|---|---|
| [`autonomy.md`](autonomy.md) | The ladder — what an agent may decide alone, per cycle, and what it takes to move up |
| [`scoreboard.md`](scoreboard.md) | The five numbers that say whether any of this is working |
| [`cycles/`](cycles/) | One file per operating cycle: what it does, when it fires, its prompt |
| [`ledger/`](ledger/) | The rows cycles emit. The scoreboard is a rollup over these |

Durable facts about the application itself live in `CLAUDE.md` at the repo
root, which every session loads automatically. Procedures live in
`.claude/skills/`.

## The shape of it

A **cycle** is a recurring piece of work with a trigger, a prompt, and an
output. Each one sits on a **rung** of the autonomy ladder, from "report to the
maintainer" up to "merge it yourself". Each one appends a **row** per run. The
rows produce the **streak** that decides when a cycle has earned the next rung,
and the **scoreboard** that says whether the thing being operated is any good.

That is the whole machine. Everything else is detail.

## Two ideas worth keeping if nothing else survives

**A cycle cannot be promoted past what something automated can score.** The
point of a high rung is that the cycle acts without being read. If the
maintainer is still reading every run, the promotion moved a label and not the
work. This is why CI's blind spots are operating-system infrastructure rather
than test hygiene — see the R4 section of `autonomy.md`.

**Separate what is established from what is hypothesis.** Agentic systems fail
confidently and quietly: a well-formed signal that carries no information,
consumed as though it did. This project has three recorded instances — a
zero-row log query read as "no booking ran", a `success=True` that did not mean
a reservation existed, an `exit(1)` recorded as failure for intended behaviour.
The `booking-postmortem` skill arrived at the discipline independently and it
belongs at the top of the operating system, not buried in one procedure.

## Current state

| Cycle | Rung | Status |
|---|---|---|
| morning post-mortem | R0 | daily since 2026-08-20, 19 documents. Has earned R1 for docs-only paths |
| ship-pr | R2 | human-invoked; capped at R2 because `main` deploys |
| PR check-ins | R2 | scheduled re-checks on an open PR |

Cycles discussed and not yet built: backlog triage, post-merge deploy
verification, doctrine maintenance, member feedback intake. Intake is R1 and
buildable now; replying to members is R5 and much later. See the end of
`autonomy.md`.
