"""Uploading the observer's snapshots to the debug-artifacts bucket.

A deliberate near-copy of ``WaldenGolfProvider._upload_bytes_to_gcs``. Calling
the provider's method instead would link the entire Reserve path into this job,
which is the one thing the observer must not do (see the package docstring), and
the racer is not being refactored days before a Friday that needs its data. The
duplication is ~25 lines of ADC plumbing against a stable API; when the racer is
extracted in phase 1 both should come here.
"""

import logging
import os

import google.auth
import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

# Generous, and only ever on the post-race path: every upload happens after the
# last snapshot is in memory, so a slow one costs nothing that matters.
UPLOAD_TIMEOUT_S = 60.0

_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]


def artifacts_bucket() -> str | None:
    """The bucket snapshots go to, or None when the job is running without one.

    Read from the environment at call time rather than through ``settings`` so
    the reason a run stored nothing is visible in one place.
    """
    return os.getenv("DEBUG_ARTIFACTS_BUCKET") or None


def upload_bytes(*, bucket_name: str, object_name: str, content_type: str, data: bytes) -> str:
    """Upload bytes to GCS using ADC and the JSON upload API.

    Returns the ``gs://`` URI for the uploaded object. Raises on failure - the
    caller decides whether one lost snapshot should end the run, and it should
    not.
    """
    credentials, _ = google.auth.default(scopes=_SCOPES)  # type: ignore[no-untyped-call]
    credentials.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
    token = credentials.token
    if not token:
        raise RuntimeError("Failed to obtain access token for GCS upload")

    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o"
    params = {"uploadType": "media", "name": object_name}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}

    with httpx.Client(timeout=UPLOAD_TIMEOUT_S) as client:
        resp = client.post(url, params=params, headers=headers, content=data)
        resp.raise_for_status()

    return f"gs://{bucket_name}/{object_name}"
