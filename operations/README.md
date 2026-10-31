# operations/

How this project's scheduled work is measured, and what that work is
authorized to do without the maintainer.

Not in `docs/`, which is served publicly as GitHub Pages.

## Contents

| | |
|---|---|
| [`scoreboard.md`](scoreboard.md) | Three metrics and their sources |
| [`routines/`](routines/) | One file per Routine: trigger, authorization, prompt |
| [`features.md`](features.md) | Feature manifest the tech-debt Routine judges legacy code against |
| [`ledger/`](ledger/) | One ledger per Routine: did it run, and what did it measure |
| [`retention.md`](retention.md) | How long each store keeps data, and what enforces it |
| [`race-reports/`](race-reports/) | One report per race morning |
| `hypotheses/`, `*-setup.md`, `design-*.md` | Runbooks and design notes, moved here from the public site by #221 |

Facts about the application itself are in `CLAUDE.md` at the repository root,
loaded automatically at the start of every session. Procedures are in
`.claude/skills/`.

## Model

A **Routine** is a scheduled trigger: a cron expression, a prompt, and a fresh
session per firing. One file here per Routine. Each run appends a **row**; rows
produce the **scoreboard**.

Which Routines are deployed, and when each fires, is listed once, in `CLAUDE.md`
(Operations), and summarised in the table under "Current state" below. Each
Routine's own file under `routines/` states its trigger and status. This file
does not repeat the list in prose, so it cannot drift in a second place.

Two of them merge their own PR without the maintainer reading it first, each
limited to named paths and gated on a green `Tests` check: the race report (one
file under `race-reports/`, bounded in `routines/race-report.md`) and the
scoreboard (`docs/scoreboard.json` and `docs/scoreboard.html`, bounded in
`routines/scoreboard.md`). The others never merge: tech debt and feature dev
open PRs for the maintainer, and cost commits nothing (`routines/cost.md`).

## Two constraints

**An automated action has to be bounded by something checkable.** The race report
Routine is bounded by a path (one file under `race-reports/`), a check (a green
`Tests` run) and a deploy filter (`ignored_files`, so `operations/` does not
redeploy). Widening what a Routine may do means naming the new bound, not removing
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
| Race report Routine | deployed; fires daily since 2026-08-20; 23 reports, 2026-08-13 to 2026-09-25; merging its own since 2026-09-22 |
| Scoreboard Routine | documented as deployed 2026-10-04 (`routines/scoreboard.md`, CLAUDE.md); `docs/**` is documented as in the deploy filter since the 2026-09-27 apply (CLAUDE.md). Live trigger and build log not checked for this table |
| Tech debt Routine | documented as deployed (CLAUDE.md); Saturdays at `0 9 * * 6` UTC (`routines/tech-debt.md`); never merges, files issues and opens a PR |
| Cost Routine | documented as created 2026-10-09, monthly on the 5th, first run 2026-11-05 (`routines/cost.md`, CLAUDE.md); billing export documented as on |
| Feature dev Routine | documented as created 2026-10-19, Saturdays 08:00 CT (`routines/feature-dev.md`, CLAUDE.md); never merges, builds one issue and opens a PR. Ledger objects exist in GCS for 2026-10-24 and 2026-10-31 |
| Scoreboard page | written, at `https://alexenos.github.io/teetime/scoreboard.html`, showing the outcome split **backfilled** from the race reports, flagged on the page itself; streak and cost unavailable |
| Ledgers | one per Routine; schemas written. 23 race-report rows backfilled from the reports (`ledger/backfill/`); CLAUDE.md documents the race report Routine writing its rows to GCS from 2026-10-01, and the scoreboard and tech-debt Routine specs say theirs go under `operations/` in the artifacts bucket. GCS objects not listed for this table. The `*.jsonl` files here have 0 lines, so they are not the live ledger |

The two known defects in the deployed race report prompt — the DST cron and the
Step 3 timeout constant — are recorded in `routines/race-report.md`. Both require
editing the Routine, which this directory cannot do.
