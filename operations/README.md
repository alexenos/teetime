# operations/

How this project's scheduled work is measured, and what that work is
authorized to do without the maintainer.

Not in `docs/`, which is served publicly as GitHub Pages.

## Contents

| | |
|---|---|
| [`scoreboard.md`](scoreboard.md) | Three metrics and their sources |
| [`cycles/`](cycles/) | One file per scheduled cycle: trigger, authorization, prompt |
| [`ledger/`](ledger/) | The rows cycles emit; input to the scoreboard |
| [`race-reports/`](race-reports/) | One report per race morning |
| `hypotheses/`, `*-setup.md`, `design-*.md` | Runbooks and design notes, moved here from the public site by #221 |

Facts about the application itself are in `CLAUDE.md` at the repository root,
loaded automatically at the start of every session. Procedures are in
`.claude/skills/`.

## Model

A **cycle** is recurring work with a trigger, a prompt and an output. Each cycle
appends a **row** per run. Rows produce the **scoreboard**.

One cycle exists: the race report, daily at 06:40 CT. It writes that morning's
report, opens a PR and merges it once `Tests` is green — the only agent action in
this project that reaches `main` without being read first. `operations/cycles/race-report.md`
states what bounds it.

## Two constraints

**An automated action has to be bounded by something checkable.** The race report
cycle is bounded by a path (one file under `race-reports/`), a check (a green
`Tests` run) and a deploy filter (`ignored_files`, so `operations/` does not
redeploy). Widening what a cycle may do means naming the new bound, not removing
the old one.

**State separately what is established and what is hypothesis.** Three recorded
failures in this repository originated in a well-formed signal that carried no
information: a zero-row log query read as "no booking ran", a `success=True` that
did not indicate a reservation, and an `exit(1)` recorded as a failure for
intended behaviour. The `race-report` skill applies this distinction to
diagnosis; it applies equally to operating material.

## Current state

| | |
|---|---|
| Race report cycle | fires daily since 2026-08-20; 23 reports, 2026-08-13 to 2026-09-25; merging its own since 2026-09-22 |
| Scoreboard | specified; no rollup built |
| Ledger | specified; files empty, nothing writes to them |

The two known defects in the deployed race report prompt — the DST cron and the
Step 3 timeout constant — are recorded in `cycles/race-report.md`. Both require
editing the Routine, which this directory cannot do.
