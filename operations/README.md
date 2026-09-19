# operations/

Operating policy for this project: how work is selected, what an agent may
decide without the maintainer, and how the result is measured.

Not in `docs/`, which is served publicly as GitHub Pages.

## Contents

| | |
|---|---|
| [`autonomy.md`](autonomy.md) | The autonomy ladder: per-cycle decision authority and the conditions for increasing it |
| [`scoreboard.md`](scoreboard.md) | The five metrics and their sources |
| [`cycles/`](cycles/) | One file per operating cycle: function, trigger, prompt |
| [`ledger/`](ledger/) | The rows cycles emit; input to the scoreboard |

Facts about the application itself are in `CLAUDE.md` at the repository root,
loaded automatically at the start of every session. Procedures are in
`.claude/skills/`.

## Model

A **cycle** is recurring work with a trigger, a prompt and an output. Each
cycle is assigned a **rung** on the autonomy ladder, from reporting to the
maintainer up to merging its own changes. Each cycle appends a **row** per
run. Rows produce the **streak** that determines promotion, and the
**scoreboard** that measures the application.

## Two constraints

**A cycle cannot be promoted beyond the point at which an automated check can
score its runs.** A high rung means the cycle acts without being read; if the
maintainer still reads every run, the rung changed and the workload did not.
This makes CI coverage a prerequisite for R4 rather than general test
maintenance. See the R4 section of `autonomy.md`.

**State separately what is established and what is hypothesis.** Three
recorded failures in this repository originated in a well-formed signal that
carried no information: a zero-row log query read as "no booking ran", a
`success=True` that did not indicate a reservation, and an `exit(1)` recorded
as a failure for intended behaviour. The `booking-postmortem` skill applies
this distinction to diagnosis; it applies equally to operating material.

## Current state

| Cycle | Rung | Status |
|---|---|---|
| morning post-mortem | R0 | daily since 2026-08-20; 19 documents |
| ship-pr | R2 | maintainer-invoked; held at R2 because `main` deploys |
| PR check-ins | R2 | scheduled re-checks on an open PR |

No cycle currently emits ledger rows and no scoreboard rollup exists. The
policy in this directory is defined; the instrumentation is not built.

Cycles specified but not built: backlog triage, post-merge deploy
verification, doctrine maintenance, member feedback intake. Intake is R1 and
buildable independently; replying to members is R5. See the final section of
`autonomy.md`.
