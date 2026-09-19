# The autonomy ladder

Defines what an agent may decide without the maintainer, per cycle, and the
conditions for increasing that authority.

Promotion requires a stated rule rather than a judgement call, so that it
happens on evidence and not on recency.

## The ladder

Rungs are defined once, by blast radius — what an action can reach — not per
cycle. A new cycle is assigned a rung. It does not define its own ladder.

| Rung | Permitted | Cannot reach |
|---|---|---|
| **R0** | Read and report. Output is text to the maintainer. | anything |
| **R1** | Write to the repository's non-shipping surface: open a draft PR, write documentation, file an issue | production, members |
| **R2** | Act on its own work in flight: push to its open PR, answer review, re-run CI | merge |
| **R3** | Merge paths that cannot deploy: `docs/`, `operations/` | Cloud Run |
| **R4** | Merge shipping code: `app/`, `terraform/` | — |
| **R5** | Act on production directly, or message a member unsupervised | — |

## Registry

| Cycle | Rung | Clean streak | Promotion rule |
|---|---|---|---|
| morning post-mortem | R0 | computed from ledger | 5 clean runs at R0 → R1, documentation paths only |
| ship-pr | R2 | computed from ledger | held at R2 until an R3 scorer exists |
| PR check-ins | R2 | computed from ledger | held at R2 |

Streaks are computed, not stored: count back through
`operations/ledger/runs.jsonl` for that cycle until the first `clean: false`. A
stored counter can diverge from the rows it summarises.

### Post-mortem cycle status

Running daily since 2026-08-20. 19 post-mortem documents produced. The
maintainer has approved the documentation PR on each occasion it was offered.

One recorded reset: on 2026-09-15 the cycle reported the morning as having no
booking when two bookings had succeeded. The cause was a log query scoped to
the service rather than the jobs, corrected in #206. The error was identified
by maintainer pushback, not by the cycle. Under the promotion rule, that reset
blocks promotion until the streak rebuilds.

## Definition of "clean"

A run is clean when its output required no correction before it could be
acted on. This is independent of the outcome being reported.

- A correctly diagnosed loss is clean.
- A correctly reported absence of data is clean.
- An incorrect diagnosis is not clean, regardless of the outcome reported.

## Scoring authority

A cycle cannot be promoted beyond the point at which an automated check can
score its runs.

At R0–R1 the maintainer scores each run at no additional cost, because the
output is read in any case. R3 and R4 exist so that the cycle acts without
being read. If the maintainer continues to score every run at those rungs, the
rung has changed without the workload changing.

| Rung | Scorer | Available |
|---|---|---|
| R0–R1 | maintainer | yes |
| R2 | CI pass/fail | yes |
| R3 | none in practice | accepted: no deploy path; the maximum failure is an incorrect document |
| R4 | CI must detect a defective deploy before members do | partial |

R3 accepts a weak scorer because its blast radius is bounded, not because the
scorer is adequate. This does not extend to R4.

### R4 scorer status

| Gap | State |
|---|---|
| mypy advisory (`continue-on-error: true`) | closed in #217 (#158) |
| browser tests reduce silently to zero and report success | closed in #217 (#159) |
| no `terraform validate` in CI | open |

While the third remains open, an R4 cycle would be scored by a check that
cannot detect infrastructure errors, and the streak would record `clean` for
runs that were not evaluated.

These three items are therefore prerequisites for R4, not general test
maintenance, and should be prioritised on that basis rather than by age or
member impact.

## Demotion

A cycle at R3 or above that causes a member-visible defect drops one rung. The
streak reset alone is insufficient.

Without demotion, the only available correction is disabling the cycle.

A member-visible defect is detected only after a member has been affected. It
is a fallback for scorer failure, not a primary control.

## Adding a cycle

1. Write `operations/cycles/<name>.md`: function, trigger, prompt, and the
   definition of "clean" for that cycle.
2. Assign rung R0.
3. Add a registry row with a promotion rule.
4. Emit a row to `operations/ledger/runs.jsonl` on every run.

Step 4 is required. A cycle that emits no rows has no computable streak and
therefore cannot be promoted.

## Member-facing cycles

R5 is the only rung that reaches a person.

**Intake and reply are separate cycles.** Recording a member's bug report or
feature request as an issue is repository-internal and sits at R1. Replying to
the member sits at R5. Combined into one cycle, the whole is gated at R5 and
the intake half cannot ship independently.

**The repository is public.** Filing a member's message verbatim as an issue
publishes its contents. Determine identifier handling before building intake;
an issue can be deleted but not removed from external indexes.
