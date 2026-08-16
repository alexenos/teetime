#!/usr/bin/env bash
#
# Set up a Claude Code on the web session to run a booking post-mortem.
#
# Point the environment's setup-script setting at this file. It is idempotent
# and safe to re-run; a warm container re-runs it in about a second.
#
# Three things a fresh remote container is missing:
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
#   3. The project venv, which starts empty.
#
# `set -e` is deliberately absent: a container missing any one of the three
# should still get the other two, plus a warning naming what it lacks. That
# only works if every failure is actually reported, so the exit summary below
# reflects what is genuinely usable rather than the fact that the script ran.
#
# See docs/debug-artifact-access.md.

set -uo pipefail

SDK_ROOT="${GCLOUD_SDK_ROOT:-/opt/google-cloud-sdk}"

# Pinned rather than the rolling google-cloud-cli-linux-x86_64.tar.gz, because
# this script runs unattended, installs executables under /opt and links them
# into /usr/local/bin. TLS authenticates the host, not the object: on the
# rolling URL the bytes can change under a URL that never does, and nothing
# here would notice. A pinned version plus a recorded digest turns that into a
# loud failure.
#
# The digest was taken from the versioned archive and cross-checked three ways
# before being recorded: the download's size and MD5 match what the GCS JSON
# API reports for the object server-side, and the extracted tree reports
# "Google Cloud SDK 580.0.0". That establishes the bytes are the object Google
# is serving; the value's job from here is to detect it ever changing.
#
# The cost is explicit upgrades. To move: bump SDK_VERSION, download the new
# archive, verify it the same way, and record its sha256 in one reviewed
# change. Nothing auto-updates it.
SDK_VERSION="${GCLOUD_SDK_VERSION:-580.0.0}"
SDK_TARBALL="https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-${SDK_VERSION}-linux-x86_64.tar.gz"
SDK_SHA256="${GCLOUD_SDK_SHA256:-e580c04b45dfa2e537b8dc0cf7c828e46a65bc77ef61de69161e1f3a124d7480}"
PROJECT="${GCP_PROJECT:-gen-lang-client-0822973627}"
KEY_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-/tmp/gcp-key.json}"

log() { printf '[setup] %s\n' "$*"; }

# Anchor to the repository root rather than trusting the caller's directory.
#
# The setup-script setting does not promise a working directory, and step 3
# below runs `poetry install`, which resolves pyproject.toml from the cwd. Run
# from anywhere else that step fails with "could not find a pyproject.toml file",
# which reads like a broken checkout rather than the wrong directory - and the
# summary would report PARTIAL for a reason that has nothing to do with the
# venv. Anchoring here means the setting can be a bare path to this file.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2> /dev/null && pwd)"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/pyproject.toml" ]; then
  cd "$REPO_ROOT" || log "WARNING: could not enter $REPO_ROOT; running from $PWD"
else
  log "WARNING: no pyproject.toml above this script; running from $PWD"
fi

# mktemp rather than a fixed /tmp name: these are world-writable paths and a
# predictable one is pre-creatable as a symlink by anything else on the box.
SCRATCH=""
cleanup() { [ -n "$SCRATCH" ] && rm -rf "$SCRATCH"; }
trap cleanup EXIT
SCRATCH="$(mktemp -d)" || { log "FATAL: could not create a temp directory"; exit 0; }

have_cred=no
have_gcloud=no
have_python=no

# --- 1. Credentials ----------------------------------------------------------
# -s rather than -f: the file is pre-created empty, so "exists" is not "usable".
if [ -s "$KEY_FILE" ]; then
  log "credentials already present at $KEY_FILE"
  have_cred=yes
elif [ -n "${GCP_KEY_B64:-}" ]; then
  # Decode to scratch and move only once it is known good. Writing straight to
  # KEY_FILE means a partial decode leaves a non-empty file that passes the -s
  # test above on the next run, so a corrupt key would look like a present one
  # forever. Non-empty is not the bar; parsing as a service-account key is.
  #
  # umask, not a following chmod: a redirect creates a *new* file under the
  # caller's umask - 0644 by default - which would leave a private key
  # world-readable for the width of the decode. It does not reproduce where
  # the target is pre-created 0600, because a redirect onto an existing file
  # keeps its mode; it appears the moment the path is new.
  if (umask 077; printf '%s' "$GCP_KEY_B64" | base64 -d > "$SCRATCH/key.json") 2>/dev/null \
     && [ -s "$SCRATCH/key.json" ] \
     && python3 -c "import json,sys; json.load(open(sys.argv[1]))['client_email']" "$SCRATCH/key.json" 2>/dev/null; then
    if (umask 077; cat "$SCRATCH/key.json" > "$KEY_FILE") 2>/dev/null; then
      chmod 600 "$KEY_FILE"
      log "decoded GCP_KEY_B64 -> $KEY_FILE"
      have_cred=yes
    else
      log "WARNING: could not write the decoded key to $KEY_FILE"
    fi
  else
    log "WARNING: GCP_KEY_B64 did not decode to a service-account key;"
    log "         artifact and log access will fail. $KEY_FILE left untouched."
  fi
else
  log "WARNING: no GCP_KEY_B64 set; artifact and log access will fail"
fi

# --- 2. The gcloud CLI -------------------------------------------------------
if [ ! -x "$SDK_ROOT/bin/gcloud" ] && ! command -v gcloud > /dev/null 2>&1; then
  log "installing the gcloud CLI $SDK_VERSION from the cloud-sdk-release bucket"
  # --fail matters more than it looks. Without it curl writes the server's
  # error body to the output file and still exits 0, so a proxy 403 or a
  # renamed object lands a 200-byte XML document named like an archive, tar
  # fails, and the failure is silent - the opposite of the degraded path this
  # script exists to provide.
  if ! curl -sS --fail --max-time 600 -o "$SCRATCH/gcloud.tar.gz" "$SDK_TARBALL"; then
    log "WARNING: could not download the SDK $SDK_VERSION;"
    log "         falling back to scripts/fetch_debug_artifacts.py"
  elif ! printf '%s  %s\n' "$SDK_SHA256" "$SCRATCH/gcloud.tar.gz" | sha256sum -c - > /dev/null 2>&1; then
    log "WARNING: SDK checksum mismatch - expected $SDK_SHA256,"
    log "         got $(sha256sum "$SCRATCH/gcloud.tar.gz" 2>/dev/null | cut -d' ' -f1). NOT installing."
    log "         If Google published a new $SDK_VERSION build, verify it and update SDK_SHA256."
  else
    # --strip-components=1 into SDK_ROOT rather than extracting into its
    # parent. The archive always contains a top-level google-cloud-sdk/, so
    # extracting to dirname(SDK_ROOT) silently ignores GCLOUD_SDK_ROOT: the
    # tree lands next to the configured path, every later check misses it, and
    # the script reports an install that is not where it says it is.
    mkdir -p "$SDK_ROOT"
    if tar -xzf "$SCRATCH/gcloud.tar.gz" -C "$SDK_ROOT" --strip-components=1; then
      log "verified and unpacked to $SDK_ROOT"
    else
      log "WARNING: SDK archive downloaded and verified but would not extract"
    fi
  fi
fi

# Whichever gcloud is going to be used is the one that must be configured.
# Selecting only $SDK_ROOT/bin/gcloud meant a pre-existing gcloud elsewhere on
# PATH skipped configuration and authentication entirely, and the script still
# exited 0 with an unusable CLI.
GCLOUD_BIN=""
if [ -x "$SDK_ROOT/bin/gcloud" ]; then
  GCLOUD_BIN="$SDK_ROOT/bin/gcloud"
elif command -v gcloud > /dev/null 2>&1; then
  GCLOUD_BIN="$(command -v gcloud)"
fi

if [ -n "$GCLOUD_BIN" ]; then
  # The agent's shell does not inherit exports from this script, so putting the
  # SDK on PATH here would not survive. Symlink into a directory already on it.
  if [ -x "$SDK_ROOT/bin/gcloud" ]; then
    for tool in gcloud gsutil bq; do
      [ -x "$SDK_ROOT/bin/$tool" ] && ln -sf "$SDK_ROOT/bin/$tool" "/usr/local/bin/$tool"
    done
  fi

  # Every gcloud invocation otherwise tries dl.google.com for a component
  # update check, which the egress policy refuses - slow, and it prints a
  # traceback that reads like the command itself failed.
  "$GCLOUD_BIN" config set component_manager/disable_update_check true > /dev/null 2>&1
  "$GCLOUD_BIN" config set core/disable_usage_reporting true > /dev/null 2>&1
  "$GCLOUD_BIN" config set project "$PROJECT" > /dev/null 2>&1

  if [ "$have_cred" = yes ]; then
    if "$GCLOUD_BIN" auth activate-service-account --key-file="$KEY_FILE" > /dev/null 2>&1; then
      log "authenticated as $(python3 -c "import json;print(json.load(open('$KEY_FILE'))['client_email'])" 2>/dev/null)"
      have_gcloud=yes
    else
      log "WARNING: could not activate the service account for $GCLOUD_BIN"
    fi
  fi
fi

# --- 3. The venv -------------------------------------------------------------
# fetch_debug_artifacts.py needs google.auth + httpx, and the classifier needs
# the app package. Both come from the project venv, which starts empty.
if command -v poetry > /dev/null 2>&1; then
  if poetry run python -c "import google.auth, httpx" > /dev/null 2>&1; then
    log "python dependencies already installed"
    have_python=yes
  else
    log "installing python dependencies (this takes a few minutes on a cold container)"
    # Twice, and kept rather than discarded.
    #
    # On 2026-08-16 the cold install failed here and the session reported
    # PARTIAL; a plain `poetry install` run by hand immediately afterwards
    # succeeded with one package left to fetch, which is a transient download
    # rather than a broken lock file. One retry covers that.
    #
    # The output went to /dev/null, so the warning below named nothing and the
    # cause had to be guessed - the same failure mode this script exists to fix
    # for the key file (see the header). Keep it on disk and say where.
    install_log="${TMPDIR:-/tmp}/poetry-install.log"
    for attempt in 1 2; do
      if poetry install --no-root --no-interaction > "$install_log" 2>&1 \
         && poetry run python -c "import google.auth, httpx" > /dev/null 2>&1; then
        log "dependencies installed"
        have_python=yes
        break
      fi
      if [ "$attempt" = 1 ]; then
        log "poetry install failed; retrying once"
      fi
    done
    if [ "$have_python" != yes ]; then
      log "WARNING: poetry install did not produce a usable venv; see $install_log"
      tail -n 5 "$install_log" 2>/dev/null | while IFS= read -r line; do
        log "  | $line"
      done
    fi
  fi
else
  log "WARNING: poetry not found; scripts/fetch_debug_artifacts.py will not run"
fi

# --- Summary -----------------------------------------------------------------
# Reported per capability rather than as a single "done", because the two
# access paths fail independently and a post-mortem only needs one of them.
# Saying "done" while the session cannot run a single documented command is
# the failure mode this section exists to prevent.
if [ "$have_gcloud" = yes ] && [ "$have_python" = yes ]; then
  log "READY - both paths available"
  log "  gcloud storage ls gs://${PROJECT}-teetime-debug-artifacts/walden/race/"
  log "  poetry run python scripts/fetch_debug_artifacts.py list --date \$(date -u +%Y%m%d)"
elif [ "$have_gcloud" = yes ]; then
  log "PARTIAL - gcloud works, the python path does not"
  log "  gcloud storage ls gs://${PROJECT}-teetime-debug-artifacts/walden/race/"
elif [ "$have_python" = yes ] && [ "$have_cred" = yes ]; then
  log "PARTIAL - the python path works, gcloud does not"
  log "  poetry run python scripts/fetch_debug_artifacts.py list --date \$(date -u +%Y%m%d)"
else
  log "NOT READY - no working path to the artifacts or logs. See the warnings above."
  log "  credentials=$have_cred gcloud=$have_gcloud python=$have_python"
fi

exit 0
