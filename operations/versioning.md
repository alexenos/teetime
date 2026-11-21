# Versioning

The application follows [Semantic Versioning](https://semver.org):
`MAJOR.MINOR.PATCH`. It was set to `1.0.0` on 2026-11-21. Earlier history has
no versions beyond `0.2.0`, which had not changed since the project began.

## Which part a PR bumps

| Part | Bumped by | Example |
|---|---|---|
| MAJOR | A change that breaks how members or the maintainer use the bot: a command removed or changed in meaning, a channel dropped | `1.4.2` → `2.0.0` |
| MINOR | A new feature: a new row in `operations/features.md`, or new behaviour a member can see | `1.4.2` → `1.5.0` |
| PATCH | A fix, a tech-debt change, or a dependency change | `1.4.2` → `1.4.3` |

The parts after the bumped one reset to zero. A PR that does more than one of
these bumps only the highest, once.

SemVer itself defines PATCH as bug fixes only. Counting tech debt and
dependency changes as PATCH is this project's convention, not part of the spec.

## Every PR that deploys bumps it

The bump goes in the PR, not after the merge. A PR deploys when it changes a
file outside the paths the Cloud Build trigger ignores (`operations/`, `docs/`,
`.claude/`, `CLAUDE.md`, `tests/`, `scripts/`; see `CLAUDE.md`, Constraints).
A PR that touches only those paths does not deploy and does not bump.
That covers the race report and scoreboard Routines, which never bump.

**Exception: Dependabot PRs do not bump.** The tech-debt Routine's Tuesday run
merges them as Dependabot opened them (`operations/routines/tech-debt.md`), and
pushing a bump onto one would change the PR it reviewed. Their changes ride in
the version of the next PR that bumps.

## Where it lives

Two places, which `tests/test_version.py` requires to be equal:

- `pyproject.toml`, `[tool.poetry] version`
- `app/__init__.py`, `__version__`, which `app/main.py` (FastAPI) and the `/`
  route in `app/api/health.py` read

The constant exists because the image installs with `poetry install --no-root`
(`Dockerfile`), so `importlib.metadata` has no `teetime` package to read at
runtime.

## Two PRs open at once

Two PRs branched from the same `main` both bump from the same version and
conflict on those two lines. Whichever merges second updates its branch from
`main` and bumps again from the new version. The version on `main` is always
the version that deployed last.

## Not yet done

- No git tags. Nothing tags a release when a merge deploys.
- Nothing enforces the bump. `tests/test_version.py` checks the two places
  agree, not that a deploying PR changed them. A check comparing the PR's
  version with `main`'s would need the base branch in CI.
