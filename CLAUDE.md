# TeeTime

A golf tee time booking bot for one club, run as an experiment in agentic
software development. Every session starts here.

Keep this file short. It holds facts a session must know **before it does
anything**, and pointers to where the detail lives. Procedure goes in
`.claude/skills/`; operating policy goes in `operations/`.

## The things that bite

**`main` deploys.** A commit on `main` triggers Cloud Build and redeploys the
live service. Branch first, always. Merging is the maintainer's call, never an
agent's.

**There are three GCP resources, not one.** A log query scoped to the service
alone returns a clean, valid, **zero-row** result on a morning when the race
actually ran — indistinguishable from a quiet morning. This cost a
misdiagnosed post-mortem on 2026-09-15.

| Resource | Type | `resource.type` |
|---|---|---|
| `teetime` | Cloud Run **service** | `cloud_run_revision` |
| `teetime-racer` | Cloud Run **job** | `cloud_run_job` |
| `teetime-observer` | Cloud Run **job** | `cloud_run_job` |

**One container holds one Chrome.** `cloud_run_memory` is 2Gi against a browser
measured at ~1GiB with a loaded tee sheet, at `cloud_run_max_instances = 1`.
Concurrent browser sessions is a real failure mode, not a theoretical one — it
took out three bookings on 2026-09-17.

**`docs/` is a public website.** It is served as GitHub Pages at
`alexenos.github.io/teetime`, and the repo itself is public. Anything written
there is published. Operating material goes in `operations/`.

## The race

The booking job fires at **06:28 CT**. The club's window nominally opens at
06:30:00 CT but the sheet actually opens a second or so later — usually
06:30:01, once as late as 06:30:02. Reservations open 7 days in advance.

A booking is confirmed by `RESERVATION_CHECK` and by nothing else.
`phase=complete, success=True` is **not** proof that a tee time was reserved.

## Before you push

```bash
poetry run pytest -q && poetry run ruff check . && \
  poetry run ruff format --check . && poetry run mypy app
```

Always `poetry run` — the local venv lives outside the repo, under Poetry's
cache. Ruff over `.`, not `app tests`; that is what CI runs.

As of #217 both mypy and the browser integration tests are **blocking** in CI.
A missing or mismatched Chrome fails the build rather than silently shrinking
the suite. `ALLOW_BROWSER_TEST_SKIP=1` is the escape hatch for an environment
that sets `CI` but genuinely has no browser.

## Operating system

How this project decides what to work on, what an agent may do without asking,
and whether any of it is working: **`operations/`**.

- `operations/autonomy.md` — the autonomy ladder and which rung each cycle sits on
- `operations/scoreboard.md` — the five numbers that say whether this is working
- `operations/cycles/` — one file per operating cycle, including its prompt
- `operations/ledger/` — the rows those cycles emit

Current rungs, as a session needs them at a glance. `operations/autonomy.md`
is authoritative if this table and it disagree.

| Cycle | Rung | May |
|---|---|---|
| morning post-mortem | **R0** | report only — no commits, no PRs |
| ship-pr | **R2** | push to its own open PR; never merge |
| PR check-ins | **R2** | same |

## Skills

- `.claude/skills/booking-postmortem/` — diagnosing a morning, and what its
  evidence does and does not establish
- `.claude/skills/ship-pr/` — opening a PR, triggering CodeRabbit (it never
  reviews this repo on its own), and working the review

## House style

Distinguish what is **established** from what is **hypothesis**, in commits, PR
bodies and docs alike. A valid-looking signal that carries no information has
caused three separate misdiagnoses here; naming the uncertainty is how they get
caught. Say what you could not verify and why.
