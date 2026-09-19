# TeeTime

A golf tee time booking bot for one club, developed agentically.

This file holds facts required before a session acts, and pointers to detail.
Keep it short. Procedure belongs in `.claude/skills/`; operating policy belongs
in `operations/`.

## Constraints

**`main` deploys.** A commit on `main` triggers Cloud Build and redeploys the
live service. Branch before committing. Merging is the maintainer's decision.

**Three GCP resources, not one.** A Cloud Logging query scoped to the service
alone returns a valid zero-row result on a morning when the race ran. That
result is indistinguishable from a morning with no booking. This produced a
misdiagnosed post-mortem on 2026-09-15.

| Resource | Type | `resource.type` |
|---|---|---|
| `teetime` | Cloud Run service | `cloud_run_revision` |
| `teetime-racer` | Cloud Run job | `cloud_run_job` |
| `teetime-observer` | Cloud Run job | `cloud_run_job` |

**One container supports one Chrome.** `cloud_run_memory` is 2Gi; a browser
with a loaded tee sheet measures ~1GiB; `cloud_run_max_instances` is 1.
Concurrent browser sessions fail. Three bookings failed this way on
2026-09-17.

**`docs/` is published.** It is served as GitHub Pages at
`alexenos.github.io/teetime`, and the repository is public. Content placed
there is public. Operating material belongs in `operations/`.

## Race timing

The booking job fires at 06:28 CT. The club's window nominally opens at
06:30:00 CT; the sheet has been observed opening at 06:30:01, once at
06:30:02. Reservations open 7 days in advance.

A booking is confirmed by `RESERVATION_CHECK`. `phase=complete, success=True`
does not establish that a tee time was reserved.

## Pre-push checks

```bash
poetry run pytest -q && poetry run ruff check . && \
  poetry run ruff format --check . && poetry run mypy app
```

Use `poetry run`: the local venv is outside the repository, under Poetry's
cache. Run ruff over `.`, not `app tests`, which is what CI runs.

As of #217, mypy and the browser integration tests are blocking in CI. A
missing or version-mismatched Chrome fails the build rather than reducing the
collected test count. `ALLOW_BROWSER_TEST_SKIP=1` restores skipping for an
environment that sets `CI` but has no browser.

## Operations

Work selection, agent decision authority, and measurement: `operations/`.

- `operations/autonomy.md` — the autonomy ladder and each cycle's rung
- `operations/scoreboard.md` — the five metrics and their sources
- `operations/cycles/` — one file per operating cycle, including its prompt
- `operations/ledger/` — the rows cycles emit

Current rungs. `operations/autonomy.md` is authoritative where it and this
table disagree.

| Cycle | Rung | Permitted |
|---|---|---|
| morning post-mortem | R0 | report only; no commits, no PRs |
| ship-pr | R2 | push to its own open PR; no merge |
| PR check-ins | R2 | as above |

## Skills

- `.claude/skills/booking-postmortem/` — morning diagnosis, and what its
  evidence establishes
- `.claude/skills/ship-pr/` — opening a PR, triggering CodeRabbit (which does
  not review this repository automatically), and processing the review

## Writing conventions

State separately what is established and what is hypothesis, in commits, PR
bodies and documentation. Three misdiagnoses in this repository originated
from a well-formed signal that carried no information. Record what was not
verified, and why.
