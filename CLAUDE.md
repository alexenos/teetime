# TeeTime

A golf tee time booking bot for one club, developed agentically.

This file holds facts required before a session acts, and pointers to detail.
Keep it short. Procedure belongs in `.claude/skills/`; operating policy belongs
in `operations/`.

## Constraints

**`main` deploys.** A commit on `main` triggers Cloud Build and redeploys the
live service. Branch before committing. Merging is the maintainer's decision,
with one exception: the race report Routine merges its own report, scoped to one
file under `operations/race-reports/` and gated on a green `Tests` check. That
exception is defined in `operations/routines/race-report.md` and nowhere else.

A second exception was **granted on 2026-09-29**: the scoreboard Routine may
merge `docs/scoreboard.json` and `docs/scoreboard.html`, on the same terms — those
two paths, a green `Tests` check, nothing else. It is defined in
`operations/routines/scoreboard.md`. That Routine is deployed as of 2026-10-04.

A commit touching only `operations/`, `docs/`, `.claude/`, `CLAUDE.md`, `tests/` or
`scripts/` does
not redeploy: the Cloud Build trigger sets
`ignored_files = ["operations/**", "docs/**", ".claude/**", "CLAUDE.md", "tests/**", "scripts/**"]`
(`terraform/main.tf`). The filter applies only when every changed file matches,
so a commit that also touches app code still deploys. A filter entry is live only
once the merge that adds it has applied; check the build log, below. The merge
adding `CLAUDE.md` itself builds, because it touches `terraform/`.

**A merge to `main` applies terraform.** `cloudbuild.yaml` runs
`terraform apply -auto-approve` as its last step, so the Cloud Build trigger both
deploys the image and reconciles infrastructure. An infrastructure change is live
once it merges and the build succeeds — there is no separate manual apply.

The exception is a commit the filter above suppresses: a change touching only
`operations/`, `docs/`, `.claude/`, `CLAUDE.md`, `tests/` or `scripts/` fires no build, so it also
applies no terraform. A
terraform change always touches `terraform/`, so it always builds.

**Verify an apply by reading the build log, not by describing the resource.**
`gcloud builds triggers describe` is denied to the session service account, but
Cloud Build writes to Cloud Logging and that account holds `roles/logging.viewer`:

```bash
gcloud logging read 'resource.type="build" AND textPayload:"Apply complete"' \
  --limit=5 --format="value(timestamp,textPayload)" --freshness=7d
```

A routine build reports `0 added, 3 changed, 0 destroyed`; a build that also
changed infrastructure reports more. #220 merged at 2026-09-27T02:06Z and the
apply at 02:11Z shows `+ "docs/**"` with `4 changed`.

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
Refer to people by label: `Member A` for the people the bot books for, and
`Rival 1` for anyone else. The labels map to people in Secret Manager:
`MEMBER_PSEUDONYM_REGISTRY` in full, readable by project owners only, and
`MEMBER_PSEUDONYM_LABELS`, the subset a session needs. Member labels are also
assigned automatically when a member connects a login (#256, `member_pseudonyms`
table), and logs show members as `<Member C>` rather than by Telegram ID. A race
report is checked before it commits; see `.claude/skills/race-report/` §8a.

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

Measurement, and the Routines that run on a schedule: `operations/`.

- `operations/scoreboard.md` — three metrics and their definitions. Published as
  a page at `alexenos.github.io/teetime/scoreboard.html` when the Routine exists.
- `operations/routines/` — one file per Routine, including its prompt and what it
  is authorized to do. A file states whether it is deployed.
- `operations/ledger/` — one ledger per Routine: did it run, and what did it
  measure. The race report writes its rows to GCS from 2026-10-01.
- `operations/race-reports/` — one report per race morning
- `operations/onboarding.md` — letting members in and out: the maintainer
  approves a join request, members connect their own Walden login (never seen
  by the maintainer), and what must be true before inviting anyone new
- `operations/credential-encryption.md` — the Cloud KMS key, who has read a
  login, and the migration off the hand-made Fernet key

Three Routines are deployed: the race report, daily at `40 11 * * *` UTC, the
scoreboard, daily at `30 12 * * *` UTC, and tech debt, Saturdays at `0 9 * * 6`
UTC (`operations/routines/tech-debt.md`). Tech debt never merges; it files issues
and opens a PR you merge. The cost Routine is specified and not deployed. The
race report and scoreboard crons move an hour on 2026-11-01. A prompt pasted into the
routines UI is read as markdown and loses paired `*`; compare the live text
after any paste (`operations/routines/scoreboard.md`, The prompt). `ship-pr` and
the PR check-ins are not Routines; they are maintainer-invoked and push only to
their own open PR.

`docs/**` has been live in `ignored_files` since #220's apply on 2026-09-27, so a
scoreboard publish does not rebuild the service.

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
