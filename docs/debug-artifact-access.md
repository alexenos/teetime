# Giving a remote session read access to the debug artifacts

A booking post-mortem runs on two things the bot leaves behind: the debug
artifacts in GCS and the Cloud Run logs. A Claude Code on the web session can
reach neither by default, which is why the 2026-08-13 post-mortem had to be
written from fixtures and code alone.

This is what closes that gap. It is read-only and scoped to one bucket and the
project's logs.

## What is not needed

- **The `gcloud` CLI.** `WaldenProvider._upload_bytes_to_gcs` already does GCS
  I/O with `google.auth` + `httpx` against the JSON API, with no
  `google-cloud-storage` dependency. `scripts/fetch_debug_artifacts.py` reads
  them back the same way, with read-only scopes. Both libraries are already in
  the venv.
- **A network policy change.** `storage.googleapis.com`,
  `logging.googleapis.com` and `oauth2.googleapis.com` are all reachable from a
  remote session today; an unauthenticated bucket request returns a genuine GCS
  `401`, not a proxy block. (`dl.google.com` *is* refused, which is why
  installing the CLI fails — but the CLI is not the path.)

The only missing piece is a credential.

## Setting it up

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

```bash
echo "$GCP_KEY_B64" | base64 -d > /tmp/gcp-key.json
chmod 600 /tmp/gcp-key.json
```

Set `GOOGLE_APPLICATION_CREDENTIALS` as an environment variable rather than
exporting it from the setup script. The agent's shell does not inherit the
script's exports, but environment-level variables are present on every call.

### 4. Verify

```bash
poetry run python scripts/fetch_debug_artifacts.py list --date 20260813
```

A listing means it works. A `403` names the missing role; a credentials error
means ADC never found the key file.

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
