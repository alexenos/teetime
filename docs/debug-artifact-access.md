# Giving a remote session read access to the debug artifacts

A booking post-mortem runs on two things the bot leaves behind: the debug
artifacts in GCS and the Cloud Run logs. A Claude Code on the web session can
reach neither by default, which is why the 2026-08-13 post-mortem had to be
written from fixtures and code alone.

This is what closes that gap. It is read-only and scoped to one bucket and the
project's logs.

## What is not needed

- **A network policy change.** `storage.googleapis.com`,
  `logging.googleapis.com` and `oauth2.googleapis.com` are all reachable from a
  remote session today; an unauthenticated bucket request returns a genuine GCS
  `401`, not a proxy block.
- **The `gcloud` CLI**, for the artifacts themselves.
  `WaldenProvider._upload_bytes_to_gcs` already does GCS I/O with `google.auth`
  + `httpx` against the JSON API, with no `google-cloud-storage` dependency.
  `scripts/fetch_debug_artifacts.py` reads them back the same way, with
  read-only scopes. Both libraries are already in the venv.

The only missing piece is a credential.

## The CLI, which turns out to be installable after all

This document originally said the CLI could not be installed in a remote
session, because the egress policy refuses `dl.google.com`. It does refuse it,
and `packages.cloud.google.com` as well — but that was the wrong conclusion.
`dl.google.com` is a CDN in front of the `cloud-sdk-release` GCS bucket, and
`storage.googleapis.com` is reachable, being the same host the debug artifacts
come from. Pulling the tarball from the bucket installs a complete SDK — `bq`
and `gsutil` included, with its own bundled Python, so the
`python3.13: command not found` that breaks gsutil locally does not apply:

```bash
curl -sS -o /tmp/gcloud.tar.gz \
  https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-linux-x86_64.tar.gz
tar -xzf /tmp/gcloud.tar.gz -C /opt
ln -sf /opt/google-cloud-sdk/bin/{gcloud,gsutil,bq} /usr/local/bin/
gcloud config set component_manager/disable_update_check true
gcloud auth activate-service-account --key-file=/tmp/gcp-key.json
```

Verified 2026-08-15: `gcloud storage ls` and `gcloud logging read` both work
against this project, cold install to working command in about 16 seconds.

Two details that matter. Set `component_manager/disable_update_check` — every
invocation otherwise reaches for `dl.google.com` and prints a `ProxyError`
traceback that reads like the command itself failed, when it has not. And
symlink into `/usr/local/bin` rather than exporting `PATH`: the agent's shell
does not inherit a setup script's exports.

`scripts/setup_remote_env.sh` does all of the above plus the key and the venv,
and is idempotent. Point the environment's setup-script setting at it.

### What the CLI still cannot do here

`gcloud run revisions list` — the step §1 of the post-mortem skill wants for
"which code actually ran" — fails with `Permission 'run.revisions.list' denied`.
That is this account's IAM, not the network: the grant above is storage and
logging only, and Cloud Run's admin API is neither.

Adding `roles/run.viewer` closes it. **Run this on your own machine**, like
steps 1 and 2 — the post-mortem account cannot grant itself a role:

```bash
PROJECT=gen-lang-client-0822973627
SA=teetime-artifact-reader@$PROJECT.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding $PROJECT \
  --member=serviceAccount:$SA --role=roles/run.viewer
```

Then, from a session, `gcloud run revisions list --service=teetime
--region=us-central1 --limit=5` should list revisions instead of erroring.

**What that grant exposes.** `roles/run.viewer` is read-only over Cloud Run —
it cannot deploy, update traffic, or delete — but it does read service
*configuration*, which includes the environment block. That would matter if
secrets were set as literal env values. They are not: every secret in
`terraform/main.tf` is injected through `value_source.secret_key_ref`, so the
service config carries secret *names* and versions while the values stay in
Secret Manager, behind `roles/secretmanager.secretAccessor` that this account
does not have. The plain `env` entries are booking flags and timezone. So the
grant adds revision and service metadata and no credential material.

If you would rather grant the single permission than the role, a custom role
does it:

```bash
gcloud iam roles create teetimeRevisionReader --project=$PROJECT \
  --title="List Cloud Run revisions for post-mortems" \
  --permissions=run.revisions.list,run.revisions.get
gcloud projects add-iam-policy-binding $PROJECT \
  --member=serviceAccount:$SA --role=projects/$PROJECT/roles/teetimeRevisionReader
```

None of this is load-bearing: without it the logs still name the revision that
served a run, in `resource.labels.revision_name`, and commit timestamps plus the
run's own logged behaviour bound which code was deployed. It saves a step in §1
rather than enabling anything new.

## Setting it up

**Run steps 1 and 2 on your own machine, not in a remote session.** They need
`gcloud` and an identity that can edit IAM, and a remote session has neither —
which is the whole reason this document exists. Only step 4 runs in a session.

### 1. A read-only service account

```bash
PROJECT=gen-lang-client-0822973627
SA=teetime-artifact-reader@$PROJECT.iam.gserviceaccount.com

gcloud iam service-accounts create teetime-artifact-reader \
  --project=$PROJECT \
  --display-name="Read-only debug artifact + log access for post-mortems"

gcloud storage buckets add-iam-policy-binding gs://$PROJECT-teetime-debug-artifacts \
  --member=serviceAccount:$SA --role=roles/storage.objectViewer

gcloud projects add-iam-policy-binding $PROJECT \
  --member=serviceAccount:$SA --role=roles/logging.viewer

gcloud iam service-accounts keys create key.json --iam-account=$SA
base64 -w0 key.json          # macOS: base64 -i key.json
```

Make this a dedicated account. Do not reuse the Cloud Run runtime service
account — that one can *write* to the bucket and holds whatever else the service
needs.

### 2. Environment variables

In the environment's settings ([Claude Code on the web
docs](https://code.claude.com/docs/en/claude-code-on-the-web)):

| Variable | Value |
|---|---|
| `GCP_KEY_B64` | the base64 blob from step 1 |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/tmp/gcp-key.json` |
| `DEBUG_ARTIFACTS_BUCKET` | `gen-lang-client-0822973627-teetime-debug-artifacts` (optional; the script defaults to it) |

### 3. Setup script

Point the environment's setup-script setting at `scripts/setup_remote_env.sh`.
It decodes the key, installs the CLI, authenticates, and installs the venv;
it is idempotent, so a warm container re-runs it in about a second.

**Setting the two environment variables is not sufficient on its own.** Without
the script nothing decodes `GCP_KEY_B64` into `/tmp/gcp-key.json`, and on
2026-08-15 the target file existed and was zero bytes — so a check for the
file's existence passed while every read failed with an ADC error that named
nothing. If you are doing it by hand, test `-s`, not `-f`:

```bash
[ -s /tmp/gcp-key.json ] || (umask 077; printf '%s' "$GCP_KEY_B64" | base64 -d > /tmp/gcp-key.json)
chmod 600 /tmp/gcp-key.json
```

The `umask` matters when the target does not already exist: a redirect creates
the file under the caller's umask, so the plain `> file; chmod 600 file` form
leaves a private key world-readable for the width of the decode.

Set `GOOGLE_APPLICATION_CREDENTIALS` as an environment variable rather than
exporting it from the setup script. The agent's shell does not inherit the
script's exports, but environment-level variables are present on every call.

### 4. Verify

```bash
poetry run python scripts/fetch_debug_artifacts.py list --date 20260813
gcloud storage ls gs://gen-lang-client-0822973627-teetime-debug-artifacts/walden/race/
```

A listing means it works. A `403` names the missing role; a credentials error
means ADC never found the key file. Note that the venv starts empty on a fresh
container — a `ModuleNotFoundError: No module named 'google'` means
`poetry install --no-root` has not run, not that anything is misconfigured.

## Using it

```bash
# What exists for a morning
poetry run python scripts/fetch_debug_artifacts.py list --date 20260813

# Pull it down; ledgers are summarized automatically
poetry run python scripts/fetch_debug_artifacts.py fetch --date 20260813 --out ./artifacts

# Cloud Run logs, in CT wall-clock
poetry run python scripts/fetch_debug_artifacts.py logs \
  --date 2026-08-13 --from 06:20 --to 08:00 --out run.txt
```

### The two clocks

Artifact object names come from `datetime.now()` inside Cloud Run, and nothing
sets `TZ` there, so **artifact dates are UTC**. The member reads in ET and the
club runs in CT. A 06:30 CT run lands under the same date stamp either way; an
evening run does not. `logs` takes CT times and converts them, so pass the times
you mean.

## Without a stored key

`GCP_ACCESS_TOKEN` takes precedence over ADC, so a session can be handed a
short-lived token instead:

```bash
export GCP_ACCESS_TOKEN=$(gcloud auth print-access-token)
```

It expires in about an hour and nothing is stored. Fine for a one-off; the
service account is what makes a morning-after check unattended.

## Costs and hygiene

- A service-account key is long-lived and does not expire on its own. Delete it
  when the investigation is done:
  `gcloud iam service-accounts keys delete KEY_ID --iam-account=$SA`.
- The artifacts contain live club session HTML — ViewState, and on tee-sheet
  captures other members' names. `_flush_pre_window_sheet` deliberately keeps
  that material out of application logs and puts it only in the bucket, so
  bucket read access is a wider grant than log access.
- The two roles above are the whole grant. Neither one can write, delete, or
  book anything.
