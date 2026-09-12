###############################################################################
# The observer job (issue #189, phase 0 of docs/design-observer-and-fanout.md)
#
# A separate Cloud Run *job* that logs in, parks on the target tee sheet and
# photographs it once a second across the booking window. It never sends a
# Reserve: nothing in app/observer/ imports a module that can, and
# tests/test_observer.py asserts it.
#
# Why its own job rather than a thread in the booking service:
#
#   * Own session. A Reserve body addresses its slot positionally
#     (teeTimeSlots:11 is "the twelfth row of whatever date this view shows")
#     and the date rides in session state, so re-pointing a shared view
#     mid-race would reserve the wrong tee time with no error at all.
#   * Own container. A co-resident Chrome was measured stealing 310ms from one
#     attempt (cpu/wall 0.39, 2026-08-28), and post-response processing is
#     already 44% of the race budget. A second browser in the racing container
#     attacks the exact three seconds that decide the morning.
#
# Why it starts at 06:24 and the racer at 06:28: concurrent sessions on one
# credential are proven fine in production, but if the club ever did start
# enforcing one session per member the *newer* login wins - so the observer
# logs in first and the casualty would be the observer, never the booking.
###############################################################################

locals {
  observer_job_name = "${local.service_name}-observer"

  # Exactly what the observer needs to log in and nothing else. The two Walden
  # secrets already carry versions (the racer reads them today), so referencing
  # them cannot fail a deploy. CREDENTIAL_ENCRYPTION_KEY is added only when
  # credential_store_enabled, for the same reason as on the service: Terraform
  # creates that secret empty and a revision referencing a version-less secret
  # fails to deploy.
  #
  # The observer resolves credentials through the same credential_service the
  # racer uses, so if the watched member ever gets a dedicated login the
  # observer follows it - which is the point, since the sheet is rendered for
  # whoever is looking at it.
  observer_secrets = concat(
    [
      "WALDEN_MEMBER_NUMBER",
      "WALDEN_PASSWORD",
    ],
    var.credential_store_enabled ? local.credential_secrets : [],
  )
}

resource "google_service_account" "observer" {
  account_id   = "${local.service_name}-observer"
  display_name = "TeeTime Observer Job Service Account"
  description  = "Read-only tee sheet observer (issue #189). No Reserve path."
}

# Deliberately not the Cloud Run service account. The observer needs two
# secrets, a bucket and the database; the service account for the racer can
# read Telegram, Twilio and Gemini credentials too, and the observer has no
# business with any of them.
resource "google_secret_manager_secret_iam_member" "observer_secret_access" {
  for_each = toset(local.observer_secrets)

  secret_id = google_secret_manager_secret.secrets[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.observer.email}"
}

# objectCreator, not objectAdmin: the observer writes new snapshots and can
# neither read nor overwrite the race artifacts stored beside them.
resource "google_storage_bucket_iam_member" "observer_debug_artifacts_writer" {
  bucket = google_storage_bucket.debug_artifacts.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.observer.email}"
}

# The observer reads the due booking to learn which date to watch, so it needs
# the same database the service writes bookings to.
resource "google_project_iam_member" "observer_sql_client" {
  count   = var.enable_cloud_sql ? 1 : 0
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.observer.email}"
}

resource "google_cloud_run_v2_job" "observer" {
  name     = local.observer_job_name
  location = var.region

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.observer.email

      # No retries. A retry would start a second run after the window has
      # closed, logging in again for a race that is already decided - and on
      # the fail-safe ordering above, a *newer* login is the one that would
      # survive if the club ever enforced single sessions. There is nothing
      # worth retrying: the morning is gone.
      max_retries = 0

      # 06:24 start, nine snapshots to 06:30:08, then nine uploads of ~670KB.
      # Fifteen minutes is generous by design - the timeout exists to stop a
      # hung browser holding an instance, not to bound the work.
      timeout = "900s"

      containers {
        image = var.container_image != "" ? var.container_image : "${var.region}-docker.pkg.dev/${var.project_id}/${local.service_name}/${local.service_name}:latest"

        # The image is the booking service's, whose CMD starts uvicorn. The
        # observer is a batch entry point, so the command is overridden here.
        command = ["python", "-m", "app.observer"]

        resources {
          limits = {
            cpu    = var.observer_cpu
            memory = var.observer_memory
          }
        }

        env {
          name  = "TIMEZONE"
          value = var.timezone
        }

        # The observer measures its offsets from the *stated* window - 06:30:00.000
        # CT - rather than the racer's aiming point, so its timestamps are an
        # independent reference frame that the racer's ledger can be checked against.
        env {
          name  = "BOOKING_OPEN_HOUR"
          value = tostring(var.booking_open_hour)
        }

        env {
          name  = "BOOKING_OPEN_MINUTE"
          value = tostring(var.booking_open_minute)
        }

        # Used only for the fallback date on a morning with no booking due, so
        # a non-Friday still yields a control-group observation.
        env {
          name  = "DAYS_IN_ADVANCE"
          value = tostring(var.days_in_advance)
        }

        env {
          name  = "DEBUG_ARTIFACTS_BUCKET"
          value = var.debug_artifacts_bucket
        }

        env {
          name  = "LOG_LEVEL"
          value = var.log_level
        }

        env {
          name  = "OBSERVER_ENABLED"
          value = tostring(var.observer_enabled)
        }

        env {
          name  = "OBSERVER_PHONE_NUMBER"
          value = var.observer_phone_number
        }

        env {
          name  = "OBSERVER_SNAPSHOT_COUNT"
          value = tostring(var.observer_snapshot_count)
        }

        env {
          name  = "OBSERVER_SNAPSHOT_INTERVAL_MS"
          value = tostring(var.observer_snapshot_interval_ms)
        }

        dynamic "env" {
          for_each = toset(local.observer_secrets)
          content {
            name = env.value
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.secrets[env.value].secret_id
                version = "latest"
              }
            }
          }
        }

        dynamic "env" {
          for_each = var.enable_cloud_sql ? [1] : []
          content {
            name  = "DATABASE_URL"
            value = "postgresql+asyncpg://${google_sql_user.teetime[0].name}:${random_password.db_password[0].result}@/${google_sql_database.teetime[0].name}?host=/cloudsql/${google_sql_database_instance.teetime[0].connection_name}"
          }
        }

        dynamic "volume_mounts" {
          for_each = var.enable_cloud_sql ? [1] : []
          content {
            name       = "cloudsql"
            mount_path = "/cloudsql"
          }
        }
      }

      dynamic "volumes" {
        for_each = var.enable_cloud_sql ? [1] : []
        content {
          name = "cloudsql"
          cloud_sql_instance {
            instances = [google_sql_database_instance.teetime[0].connection_name]
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.observer_secret_access,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "observer_scheduler_invoker" {
  name     = google_cloud_run_v2_job.observer.name
  location = google_cloud_run_v2_job.observer.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# 06:24, four minutes before the racer's 06:28. See the ordering note at the top
# of this file: this is a safety property, not a preference. Changing this to
# run at or after 06:28 would make the booking the casualty of any future
# single-session enforcement, and starting later than 06:24 leaves a cold start
# no room to finish logging in before the racer does.
resource "google_cloud_scheduler_job" "observe_window" {
  count = var.observer_enabled ? 1 : 0

  name        = "${local.service_name}-observe-window"
  description = "Photograph the tee sheet across the booking window (read-only, issue #189)"
  schedule    = var.observer_schedule
  time_zone   = var.timezone

  # Only the request that starts the execution. The job runs past it, and its
  # own timeout above is what bounds the work.
  attempt_deadline = "60s"

  # No retries, and this is the opposite of the usual instinct. `jobs:run`
  # creates the execution and returns immediately, so a request that started an
  # execution but whose acknowledgement was lost in transit would be retried
  # into a *second* execution - Cloud Run has no singleton guarantee for jobs.
  # That means two browsers and two logins on the shared credential inside the
  # booking window, and the newer of them is the one that survives any future
  # single-session enforcement.
  #
  # The asymmetry decides it: a duplicate execution can disturb a real booking,
  # while a start that never happened costs one morning of data that is free
  # anyway - and there is another one tomorrow.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.observer.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job_iam_member.observer_scheduler_invoker,
  ]
}
