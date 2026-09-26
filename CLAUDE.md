# TeeTime

A golf tee time booking bot for one club, developed agentically.

This file holds facts required before a session acts, and pointers to detail.
Keep it short. Procedure belongs in `.claude/skills/`; operating policy belongs
in `operations/`.

## Constraints

**`main` deploys.** A commit on `main` triggers Cloud Build and redeploys the
live service. Branch before committing. Merging is the maintainer's decision,
with one exception: the race report cycle merges its own report, scoped to one
file under `operations/race-reports/` and gated on a green `Tests` check. That
exception is defined in `operations/cycles/race-report.md` and nowhere else.

A commit touching only `operations/` does not redeploy: the Cloud Build trigger
sets `ignored_files = ["operations/**"]` (`terraform/main.tf`). The filter
applies only when every changed file matches, so a commit that also touches app
code still deploys.

**Three GCP resources, not one.** A Cloud Logging query scoped to the service
alone returns a valid zero-row result on a morning when the race ran. That
result is indistinguishable from a morning with no booking. This produced a
misdiagnosed race report on 2026-09-15.

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
there is public, and now holds only the site itself — index, privacy, terms.
Operating material belongs in `operations/`, where #221 moved it. This applies
to member identifiers too: do not write names, phone numbers or Telegram
handles anywhere in the repository.

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

Measurement and the scheduled cycles: `operations/`.

- `operations/scoreboard.md` — three metrics and their sources
- `operations/cycles/` — one file per scheduled cycle, including its prompt
  and what it is authorized to do
- `operations/ledger/` — the rows cycles emit; specified, not yet written
- `operations/race-reports/` — one report per race morning

One cycle runs on a schedule: the race report, daily at `40 11 * * *` UTC.
`ship-pr` and the PR check-ins are maintainer-invoked and push only to their
own open PR.

## Skills

- `.claude/skills/race-report/` — morning diagnosis, and what its evidence
  establishes
- `.claude/skills/ship-pr/` — opening a PR, triggering CodeRabbit (which does
  not review this repository automatically), and processing the review.
  `.coderabbit.yaml` excludes `operations/race-reports/**` from review (#223).

## Writing conventions

State separately what is established and what is hypothesis, in commits, PR
bodies and documentation. Three misdiagnoses in this repository originated
from a well-formed signal that carried no information. Record what was not
verified, and why.
