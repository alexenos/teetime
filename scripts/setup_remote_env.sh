#!/usr/bin/env bash
#
# Set up a Claude Code on the web session to run a booking post-mortem.
#
# Point the environment's setup-script setting at this file. It is idempotent
# and safe to re-run; a warm container re-runs it in about a second.
#
# Two things a fresh remote container is missing:
#
#   1. The service-account key. The environment holds it as GCP_KEY_B64 and
#      points GOOGLE_APPLICATION_CREDENTIALS at /tmp/gcp-key.json, but nothing
#      decodes one into the other. On 2026-08-15 the target existed and was
#      zero bytes, which surfaces as an ADC error several steps into a
#      post-mortem rather than as anything that names the cause.
#
#   2. The gcloud CLI. docs/debug-artifact-access.md used to say installing it
#      was impossible here because the egress policy refuses dl.google.com. It
#      refuses dl.google.com, but that host is only a CDN in front of the
#      cloud-sdk-release bucket, and storage.googleapis.com is reachable -
#      it is the same host the debug artifacts come from. Pulling the tarball
#      from the bucket directly installs a complete SDK, gsutil and bq
#      included, with its own bundled Python.
#
# See docs/debug-artifact-access.md.

set -uo pipefail

SDK_ROOT="${GCLOUD_SDK_ROOT:-/opt/google-cloud-sdk}"
SDK_TARBALL="https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-linux-x86_64.tar.gz"
PROJECT="${GCP_PROJECT:-gen-lang-client-0822973627}"
KEY_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-/tmp/gcp-key.json}"

log() { printf '[setup] %s\n' "$*"; }

# --- 1. Credentials ----------------------------------------------------------
# -s rather than -f: the file is pre-created empty, so "exists" is not "usable".
if [ -s "$KEY_FILE" ]; then
  log "credentials already present at $KEY_FILE"
elif [ -n "${GCP_KEY_B64:-}" ]; then
  # umask in a subshell, not chmod afterwards. A redirect creates a *new* file
  # under the caller's umask - 0644 by default - so `> key; chmod 600 key`
  # leaves a private key world-readable for the width of the decode. It does
  # not show up in the environment this was written for, because there the
  # file is pre-created 0600 and a redirect onto an existing file keeps its
  # mode; it appears the moment GOOGLE_APPLICATION_CREDENTIALS points
  # somewhere new. The chmod stays for that pre-existing case, where the mode
  # is whatever someone else set.
  if (umask 077; printf '%s' "$GCP_KEY_B64" | base64 -d > "$KEY_FILE") 2>/dev/null && [ -s "$KEY_FILE" ]; then
    chmod 600 "$KEY_FILE"
    log "decoded GCP_KEY_B64 -> $KEY_FILE"
  else
    log "WARNING: GCP_KEY_B64 did not decode; artifact and log access will fail"
    rm -f "$KEY_FILE"
  fi
else
  log "WARNING: no GCP_KEY_B64 set; artifact and log access will fail"
fi

# --- 2. The gcloud CLI -------------------------------------------------------
if command -v gcloud > /dev/null 2>&1; then
  log "gcloud already on PATH"
elif [ -x "$SDK_ROOT/bin/gcloud" ]; then
  log "gcloud already installed at $SDK_ROOT"
else
  log "installing the gcloud CLI from the cloud-sdk-release bucket"
  if curl -sS --max-time 600 -o /tmp/gcloud.tar.gz "$SDK_TARBALL"; then
    mkdir -p "$(dirname "$SDK_ROOT")"
    tar -xzf /tmp/gcloud.tar.gz -C "$(dirname "$SDK_ROOT")" && log "unpacked to $SDK_ROOT"
    rm -f /tmp/gcloud.tar.gz
  else
    log "WARNING: could not download the SDK; use scripts/fetch_debug_artifacts.py instead"
  fi
fi

# The agent's shell does not inherit exports from this script, so putting the
# SDK on PATH here would not survive. Symlink into a directory already on it.
if [ -x "$SDK_ROOT/bin/gcloud" ]; then
  for tool in gcloud gsutil bq; do
    [ -x "$SDK_ROOT/bin/$tool" ] && ln -sf "$SDK_ROOT/bin/$tool" "/usr/local/bin/$tool"
  done

  # Every gcloud invocation otherwise tries dl.google.com for a component
  # update check, which the egress policy refuses - slow, and it prints a
  # traceback that reads like the command itself failed.
  "$SDK_ROOT/bin/gcloud" config set component_manager/disable_update_check true > /dev/null 2>&1
  "$SDK_ROOT/bin/gcloud" config set core/disable_usage_reporting true > /dev/null 2>&1
  "$SDK_ROOT/bin/gcloud" config set project "$PROJECT" > /dev/null 2>&1

  if [ -s "$KEY_FILE" ]; then
    if "$SDK_ROOT/bin/gcloud" auth activate-service-account --key-file="$KEY_FILE" > /dev/null 2>&1; then
      log "authenticated as $(python3 -c "import json;print(json.load(open('$KEY_FILE'))['client_email'])" 2>/dev/null)"
    else
      log "WARNING: could not activate the service account"
    fi
  fi
fi

# --- 3. The venv -------------------------------------------------------------
# fetch_debug_artifacts.py needs google.auth + httpx, and the classifier needs
# the app package. Both come from the project venv, which starts empty.
if command -v poetry > /dev/null 2>&1; then
  if poetry run python -c "import google.auth, httpx" > /dev/null 2>&1; then
    log "python dependencies already installed"
  else
    log "installing python dependencies (this takes a few minutes on a cold container)"
    poetry install --no-root --no-interaction > /dev/null 2>&1 \
      && log "dependencies installed" \
      || log "WARNING: poetry install failed; run it by hand"
  fi
fi

log "done - verify with: gcloud storage ls gs://${PROJECT}-teetime-debug-artifacts/walden/race/"
exit 0
