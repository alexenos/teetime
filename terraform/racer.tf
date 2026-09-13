###############################################################################
# The racer job (issue #184, Part B of docs/design-observer-and-fanout.md)
#
# The 6:30 AM race, as a Cloud Run *job* whose tasks each race one requester's
# bookings in their own container. It replaces the booking service's
# /jobs/execute-due-bookings run, which raced every requester one after another
# in one process: on 2026-09-13 the second requester's Reserves went out about
# 74 seconds into the window.
#
# Why separate containers rather than concurrent sessions in one process:
#
#   * CPU. Every requester's race lands on the same three seconds. A
#     co-resident Chrome was measured taking 310ms from one attempt (cpu/wall
#     0.39, 2026-08-28), and a single session already used up to 580ms of
#     container CPU per response on 2026-09-13.
#   * Blast radius. One crashed browser or timeout costs one requester's
#     booking, not everyone's.
#
# How tasks split the work: every task starts, and each one claims the next
# unclaimed (date, requester) group with a conditional UPDATE
# (DatabaseService.claim_next_due_group). Two tasks cannot claim the same group,
# and tasks with nothing left to claim exit in seconds. So task_count is a
# ceiling on requesters per morning, not a count of them.
#
# Toggle with racer_fanout_enabled. Off restores the service's 06:28 scheduler
# entry, which is the rollback path.
###############################################################################

locals {
  racer_job_name = "${local.service_name}-racer"
}

resource "google_cloud_run_v2_job" "racer" {
  name     = local.racer_job_name
  location = var.region

  template {
    # Every task starts at once. Anything less would queue a requester's race
    # behind another's, which is the problem this job exists to remove.
    parallelism = var.racer_max_requesters
    task_count  = var.racer_max_requesters

    template {
      # The service's own account: the racer needs exactly what the booking
      # path in the service needs - the requester credential store, the
      # messaging channel to report results, the database and the artifacts
      # bucket.
      service_account = google_service_account.cloud_run.email

      # No retries. A retried task would start after the window, and its claim
      # is already spent, so it would find nothing to race anyway.
      max_retries = 0

      # Trigger at 06:25, a ~2 minute cold start, a hold to 06:28, then up to
      # 300s of race per booking in the group (BOOKING_EXECUTION_TIMEOUT_SECONDS)
      # and the reporting. Must stay shorter than INTERRUPTED_MIN_AGE in
      # app/services/booking_service.py, so a claim can only look orphaned once
      # the container that made it is certainly gone.
      timeout = "1500s"

      containers {
        image = var.container_image != "" ? var.container_image : "${var.region}-docker.pkg.dev/${var.project_id}/${local.service_name}/${local.service_name}:latest"

        # The image is the booking service's, whose CMD starts uvicorn.
        command = ["python", "-m", "app.racer"]

        resources {
          limits = {
            cpu    = var.racer_cpu
            memory = var.racer_memory
          }
        }

        # Every booking-path flag, from the same list the service reads, so a
        # flag changed in variables.tf can never reach one and not the other.
        dynamic "env" {
          for_each = local.booking_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = toset(local.runtime_secrets)
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
    google_secret_manager_secret_iam_member.cloud_run_secret_access,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "racer_scheduler_invoker" {
  name     = google_cloud_run_v2_job.racer.name
  location = google_cloud_run_v2_job.racer.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "race_window" {
  count = var.racer_fanout_enabled ? 1 : 0

  name        = "${local.service_name}-race-window"
  description = "Race the 6:30 AM booking window, one job task per requester (issue #184)"
  schedule    = var.racer_schedule
  time_zone   = var.timezone

  # Only the request that starts the execution; the tasks run past it.
  attempt_deadline = "60s"

  # No retries, for the same reason as the observer's entry: `jobs:run` returns
  # as soon as the execution exists, so a lost acknowledgement retried would
  # start a second execution. The claim keeps that from racing any booking
  # twice, but it would still put a second set of containers into the window.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.racer.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job_iam_member.racer_scheduler_invoker,
  ]
}
