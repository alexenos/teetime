# Versioning

The application follows [Semantic Versioning](https://semver.org):
`MAJOR.MINOR.PATCH`. It was set to `1.0.0` on 2026-11-21. Earlier history has
no versions beyond `0.2.0`, which had not changed since the project began.

## Which part a PR bumps

| Part | Bumped by | Example |
|---|---|---|
| MAJOR | A change that breaks how members or the maintainer use the bot: a command removed or changed in meaning, a channel dropped | `1.4.2` → `2.0.0` |
| MINOR | A new feature: a new row in `operations/features.md`, or new behaviour a member can see | `1.4.2` → `1.5.0` |
| PATCH | A fix, a tech-debt change, a dependency change, or any other change that deploys (`README.md`, `terraform/`, config) | `1.4.2` → `1.4.3` |

The parts after the bumped one reset to zero. A PR that does more than one of
these bumps only the highest, once.

SemVer itself defines PATCH as bug fixes only. Counting tech debt and
dependency changes as PATCH is this project's convention, not part of the spec.

## Every PR that deploys bumps it

The bump goes in the PR, not after the merge. A PR deploys when it changes a
file outside the paths the Cloud Build trigger ignores (`operations/`, `docs/`,
`.claude/`, `CLAUDE.md`, `tests/`, `scripts/`, `.github/`; see `CLAUDE.md`, Constraints).
A PR that touches only those paths does not deploy and does not bump.
That covers the race report and scoreboard Routines, which never bump.

**Exception: Dependabot PRs do not bump.** The tech-debt Routine's Tuesday run
merges them as Dependabot opened them (`operations/routines/tech-debt.md`), and
pushing a bump onto one would change the PR it reviewed. A Dependabot merge
still redeploys, under the unchanged version, so one version can name more than
one deployed build. The image tag is the commit sha (`cloudbuild.yaml`); use it
when the exact build matters.

## Where it lives

`app/__init__.py`, `__version__`. `app/main.py` (FastAPI) and the `/` route in
`app/api/health.py` read it. A bump changes that one line.

`pyproject.toml` also has a `version`, which Poetry requires. It is frozen at
`1.0.0` and never bumped, and `tests/test_version.py` fails if it changes. The
`Dockerfile` copies `pyproject.toml` before `poetry install`, so editing it on
every deploy would rebuild the cached dependency layer that #424 keeps between
deploys. Reading the version from package metadata is not an option either: the
image installs with `--no-root`, so there is no `teetime` package to read.

## Two PRs open at once

Two PRs branched from the same `main` both bump from the same version. If
they bump to different values they conflict on that line. If they bump to the
same value (both `1.0.0` → `1.0.1`), git merges the identical lines with no
conflict, and the second PR would deploy under the first one's version.

So whichever merges second, conflict or not, updates its branch from `main`
and compares: if its version is not ahead of `main`'s, it bumps again from
`main`'s. The version on `main` is then always the version of the last PR
that bumped it.

## Not yet done

- No git tags. Nothing tags a release when a merge deploys.
- Nothing in CI enforces the bump. `.claude/skills/ship-pr/` §3 asks for it
  when a PR is opened. A CI check comparing the PR's version with `main`'s
  would need the base branch in CI.
- The feature-dev and tech-debt Routines learn the rule from `CLAUDE.md` and
  the ship-pr skill, not from their trigger prompts. Not yet observed in a run.
