# Dependency updates

How each thing the service runs is kept current, and who is told when one has a
known vulnerability. Research for #446, 2026-11-15.

**Status: recommendations 1–3 adopted by the maintainer on 2026-11-16; 4–6 not
adopted for now. On 2026-11-17, 3 (no auto-merge) was replaced by a reviewed,
gated weekly merge by the tech-debt Routine's Tuesday run** (see Decision, at the
end). Dependabot alerts and security
updates are on, as of 2026-11-17. Grouping and version updates take effect when
`.github/dependabot.yml` merges.

## What was found on 2026-11-15

Established, by running the tools below on this date:

- **Dependabot alerts and security updates are both off.** The repository API
  reports `dependabot_security_updates: disabled`, and
  `GET /repos/alexenos/teetime/dependabot/alerts` returns 403, "Dependabot alerts
  are disabled for this repository". Secret scanning and push protection are on.
  The repository is public, so none of this was ever reporting anything.
- **15 of the 74 packages in the image have published advisories, 103 distinct
  in all.** `pip-audit 2.10.1`, run against every pin in `poetry.lock` with
  `--no-deps --disable-pip`, against both the PyPI and the OSV databases (same
  packages from each). Three more packages are dev-only (pygments, pytest,
  soupsieve), with 10 advisories between them, for 113 across the whole lock.
  Counts are distinct advisory ids per package, from the OSV run.

  | Package (locked) | In the image because | Highest GHSA severity | Clears all at |
  |---|---|---|---|
  | starlette 0.46.2 | fastapi | HIGH | 1.3.1 |
  | python-multipart 0.0.20 | direct | HIGH | 0.0.31 |
  | urllib3 2.6.2 | selenium, requests | HIGH | 2.8.0 |
  | aiohttp 3.13.2 | twilio, discord-py | HIGH | 3.14.4 |
  | cryptography 44.0.3 | direct | HIGH | 50.0.0 |
  | pyjwt 2.10.1 | twilio | **CRITICAL** | 2.15.0 |
  | pyasn1, anyio, certifi, idna, mako, multidict, pydantic-settings, python-dotenv, requests | various | not looked up | — |

  Severities are `database_specific.severity` from `api.osv.dev` for each GHSA.
- **The starlette fixes need a fastapi bump.** fastapi 0.115.14 requires
  `starlette >=0.40.0,<0.47.0` (`poetry.lock`); every starlette fix is 0.47.2 or
  later. A tool that only bumps the vulnerable package cannot fix this one.
  fastapi, starlette and python-multipart serve the webhook, so this is the
  most reachable code in the service.
- **python:3.11-slim is Debian 13 (trixie).** Its Docker Hub digest equals
  `3.11-slim-trixie`'s. Trixie has security support to 2028-08-09
  (endoflife.date).
- **Terraform 1.6 and google provider 5.x are well behind.** Current Terraform
  is 1.17.2 (HashiCorp checkpoint API); endoflife.date lists only the newest two
  minors as supported, so 1.6 has been unsupported for some time. The google
  provider's current release is 8.11.0, three majors past `~> 5.0`.
- **Python 3.11 reaches end of life 2027-10-31** (endoflife.date).
- **Builds per month:** 14 in October, 11 by 2026-11-15 (`Apply complete` lines in
  Cloud Build logs). Each build pushes a new digest, because `COPY . .` changes.

Hypothesis, not checked:

- The pyjwt CRITICAL (GHSA-ffc3-869f-jxw9, an algorithm-confusion bypass when
  *verifying* tokens with a public key) is probably unreachable: the app imports
  twilio's `RequestValidator` and `Client`, which sign webhooks with HMAC and do
  not verify JWTs. Not traced through twilio's code.
- Which advisories are reachable from the webhook or racer, for any package
  other than pyjwt. None was traced.
- What is in the image beyond Python: the Debian and Chromium packages were not
  scanned, because the only free scanner that reads them (`osv-scanner scan
  image`) was not run, and Artifact Registry scanning costs money (below).

## What each tool covers

From the documentation, read 2026-11-15:

| Surface | Dependabot | Renovate | pip-audit / osv-scanner in CI | Artifact Registry scanning |
|---|---|---|---|---|
| Python packages, `poetry.lock` | yes: "Dependabot supports Poetry v2" (ecosystem `pip`) | `poetry` manager | pip-audit: needs a requirements list; osv-scanner reads `poetry.lock` and `uv.lock` directly | yes, Python listed |
| Poetry itself (`1.7.1` pin) | no | no (a regex manager could) | no | no |
| `FROM python:3.11-slim` | ecosystem `docker`, parses SemVer tags; digest tracking not documented on the page read | `dockerfile` | osv-scanner can scan the built image | sees the result, not the tag |
| Debian packages and Chromium (`apt-get`) | **no** | **no** manager listed | osv-scanner `scan image` reads `/var/lib/dpkg/status` | **yes**, Debian 11–15 listed |
| Terraform providers | ecosystem `terraform`, Terraform `>= 0.13, <= 1.15.x` | `terraform` | no | no |
| Terraform CLI (`1.6` in two places) | no | `terraform-version` reads only a `.terraform-version` file | no | no |
| GitHub Actions | ecosystem `github-actions` | `github-actions` | no | no |
| Cloud Build builder images | no | `cloudbuild` | no | no |

Sources: GitHub, "Supported ecosystems and repositories" and "Dependabot options
reference"; docs.renovatebot.com/modules/manager; pypi.org/project/pip-audit;
google.github.io/osv-scanner "Supported languages and lockfiles"; Google Cloud,
"Container scanning overview", "Control your vulnerability scanning costs" and
"Artifact Analysis pricing".

Points from the same pages that matter here:

- Dependabot security-update PRs have "a separate, fixed limit of 10 open pull
  requests"; `open-pull-requests-limit` governs version updates only, and
  `0` "temporarily disable[s] version updates". Security updates ignore the
  version-update cooldown (default 3 days). `groups` with
  `applies-to: security-updates` batches security fixes into one PR.
- Dependabot options for a `target-branch` other than the default no longer apply
  to security updates, which always target the default branch.
- Artifact Registry scanning is $0.26 per scanned image, billed from the moment
  the Container Scanning API is enabled, and can be enabled per repository.
  Images "pulled within the last 30 days" keep being re-checked against new
  advisories. Whether that re-check is billed is not stated on the pages read.
  At 11–25 pushes a month that is roughly **$3–7 a month**, an estimate from the
  build counts above, not a bill.

## Recommendation

1. **Turn on Dependabot alerts and security updates now.** Free on a public
   repository, and the only option that tells the maintainer by email without
   a Routine. Add `.github/dependabot.yml` with one group per ecosystem using
   `applies-to: security-updates`, so a burst of advisories arrives as one PR per
   ecosystem and not one per package. That is the CodeRabbit concern from #446:
   one review an hour, shared.
2. **Version updates: one weekly PR for Python, monthly for Actions, nothing else.**
   `pip` weekly, all minor and patch updates in one group; `github-actions`
   monthly, one group; `docker` and `terraform` at `open-pull-requests-limit: 0`.
   Python's `open-pull-requests-limit: 2` caps version-update PRs in total, so
   majors, which fall outside the group, are limited in number, not serialized.
   The base image and the provider are major-version moves that need a human
   (the next section).
3. **No auto-merge** (replaced 2026-11-17; see Decision). Every Dependabot PR goes through the maintainer like any
   other. Merging one deploys and applies terraform. Granting a merge exception
   would be a third entry beside the race report and scoreboard in CLAUDE.md;
   this recommends against it, because a Python update is exactly what breaks the
   race path (Selenium, urllib3, chromedriver), and the only end-to-end signal is
   the next morning's race report.
4. **Clear the backlog before any blocking audit.** A blocking pip-audit step
   would fail every PR today. After the backlog is cleared, add `osv-scanner`
   on `poetry.lock` to CI as a blocking step, with reviewed exceptions kept in
   its config file next to a reason. osv-scanner over pip-audit, because it reads
   the lockfile directly, so it keeps working through #386 (uv).
5. **The image: enable Artifact Registry scanning on the `teetime` repository
   only.** It is the only option that sees Chromium and the Debian packages
   actually shipped. About $3–7 a month. The cheaper alternative is a weekly
   `osv-scanner scan image` step, either in a Routine or in Cloud Build. It costs
   nothing, but it needs Docker where it runs, and nobody has tried it here.
6. **End of life: a quarterly look, added to the tech-debt Routine's checks**
   rather than a new Routine. Python 3.11 (2027-10-31), Debian trixie
   (2028-08-09), the google provider major and the Terraform CLI.

### What is urgent

A same-day fix and deploy, not the weekly batch, when **all** of these hold:

- the advisory is CRITICAL, or HIGH with a network attack vector;
- the package is in the image (main group in `poetry.lock`, or the Debian layer);
- it is reachable from the webhook (fastapi, starlette, python-multipart,
  uvicorn, twilio) or the racer (selenium, urllib3, Chromium, chromedriver).

Who is told: GitHub "sends notifications about new alerts by email to people
with write, maintain, or admin permissions" ("Configuring notifications for
Dependabot alerts"), and only those who watch the repository with "All
Activity" or a custom setting that includes "Security alerts" ("Managing
security notifications"). So the maintainer, provided both hold. Not verified:
the maintainer's watch setting. Reading it needs the `notifications` scope,
which the session's `gh` token does not have. Check it under the repository's
Watch menu. A same-day deploy still avoids 06:20–06:45 CT on race mornings.
On that bar, the starlette and python-multipart findings above qualify,
provided one of their HIGH advisories has a network vector; that was not
checked for each one.

### Per surface

"With 1–3" is what is in place under the decision below; "If 4–6 are adopted"
is what the deferred recommendations would add.

| Surface | With 1–3 (adopted) | If 4–6 are adopted |
|---|---|---|
| Python packages | Dependabot security PRs as published, one grouped; a weekly grouped version PR | osv-scanner blocking in CI (4) |
| Poetry 1.7.1 | left alone; #153 and #386 decide it | — |
| Base image tag | **nothing**: the floating tag picks up rebuilds, never a new Python minor | Artifact Registry scanning shows its CVEs (5); the tag moves in the quarterly EOL review (6) |
| Debian packages, Chromium | the weekly `CHROMIUM_REFRESH` rebuild (#444), **unwatched**: no scan sees them | Artifact Registry scanning (5) |
| Terraform providers | **nothing**; a major move is a deliberate PR. A `.terraform.lock.hcl` is a separate decision | quarterly EOL review (6) |
| Terraform CLI | **nothing**; `tests/test_terraform_ci.py` keeps both pins in step | quarterly EOL review (6) |
| GitHub Actions | Dependabot: security PRs grouped, version updates monthly, grouped | — |
| Cloud Build builders | left floating; Google maintains `cloud-builders/docker`, and the terraform image moves with the CLI | — |

**Each Actions update redeploys.** `.github/**` is not in the Cloud Build
`ignored_files`, so merging a Dependabot PR that touches only
`.github/workflows/` rebuilds the image, redeploys, and runs `terraform apply`.
Merging one outside 06:20–06:45 CT on race mornings keeps that harmless. Adding
`.github/**` to the filter is a terraform change, left to its own PR.

## Decision

**2026-11-16, maintainer: adopt 1–3 now.**

- **Adopted:** Dependabot alerts and security updates, grouped per ecosystem (1);
  weekly grouped Python version updates and monthly grouped Actions updates, with
  Docker and Terraform version updates off (2); no auto-merge (3).
- **Not adopted for now:** the blocking CI audit (4), Artifact Registry scanning
  (5) and the quarterly end-of-life review (6). They are deferred, not rejected.
  Until 5 is adopted, nothing shows CVEs in the Debian and Chromium packages.
  Until 6 is adopted, Terraform 1.6, the google provider 5.x and Python 3.11's
  end of life have no scheduled check.

Dependabot gains no merge authority: its PRs go through the maintainer like any
other, so it adds no entry to `operations/routines/` or the CLAUDE.md merge
exceptions. Turning on alerts and security updates is a repository setting that
the maintainer changes. Version updates take effect once a
`.github/dependabot.yml` merges.

`.github/dependabot.yml` was added in the same change as this decision. It
validates against the SchemaStore `dependabot-2.0` schema.

The maintainer turned the repository settings on before 2026-11-17T00:17Z. The
API reports `dependabot_security_updates: enabled`, there are at least 100 open
alerts, and the first security PRs (#451, #452) opened at 00:17Z and 00:18Z, one
per package, because the grouping config had not merged yet.

Clearing the existing backlog of 103 image advisories is a separate piece of work,
outside this decision.

**2026-11-17, maintainer: recommendation 3 is replaced.** The maintainer wants
a code review before every merge, not a manual merge for its own sake. The
tech-debt Routine gains a Tuesday run, at 13:00 CT, three hours after Dependabot's
10:00 run. It reviews open Dependabot PRs with Claude and with CodeRabbit, and
merges at most one a week. Only lockfile or workflow paths, no majors, the head sha
reviewed by both, every check green. This is CLAUDE.md's third merge exception,
bounded in `operations/routines/tech-debt.md` (Tuesday: Dependabot PRs). Majors,
and anything that fails a gate, still wait for the maintainer.
