terraform {
  required_version = ">= 1.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  service_name = "teetime"
  apis = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
  ]
  secrets = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "GEMINI_API_KEY",
    "WALDEN_MEMBER_NUMBER",
    "WALDEN_PASSWORD",
    "SCHEDULER_API_KEY",
    "USER_PHONE_NUMBER",
  ]
}

data "google_project" "project" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "teetime" {
  location      = var.region
  repository_id = local.service_name
  description   = "Docker repository for TeeTime application"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "secrets" {
  for_each = toset(local.secrets)

  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "cloud_run" {
  account_id   = "${local.service_name}-run"
  display_name = "TeeTime Cloud Run Service Account"
}

resource "google_secret_manager_secret_iam_member" "cloud_run_secret_access" {
  for_each = toset(local.secrets)

  secret_id = google_secret_manager_secret.secrets[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_cloud_run_v2_service" "teetime" {
  name     = local.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run.email

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = var.cloud_run_max_instances
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${local.service_name}/${local.service_name}:latest"

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
      }

      env {
        name  = "TIMEZONE"
        value = var.timezone
      }

      env {
        name  = "BOOKING_OPEN_HOUR"
        value = tostring(var.booking_open_hour)
      }

      env {
        name  = "BOOKING_OPEN_MINUTE"
        value = tostring(var.booking_open_minute)
      }

      env {
        name  = "DAYS_IN_ADVANCE"
        value = tostring(var.days_in_advance)
      }

      dynamic "env" {
        for_each = toset(local.secrets)
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

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.cloud_run_secret_access,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_access" {
  name     = google_cloud_run_v2_service.teetime.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_service_account" "cloud_build" {
  account_id   = "${local.service_name}-build"
  display_name = "TeeTime Cloud Build Service Account"
}

resource "google_project_iam_member" "cloud_build_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

resource "google_project_iam_member" "cloud_build_sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

resource "google_project_iam_member" "cloud_build_logs_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

resource "google_artifact_registry_repository_iam_member" "cloud_build_writer" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.teetime.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.cloud_build.email}"
}

resource "google_service_account" "scheduler" {
  account_id   = "${local.service_name}-scheduler"
  display_name = "TeeTime Cloud Scheduler Service Account"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_service.teetime.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "execute_bookings" {
  name             = "${local.service_name}-execute-bookings"
  description      = "Execute due tee time bookings"
  schedule         = "* * * * *"
  time_zone        = var.timezone
  attempt_deadline = "300s"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.teetime.uri}/jobs/execute-due-bookings"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_project_service.apis]
}

resource "random_password" "db_password" {
  count   = var.enable_cloud_sql ? 1 : 0
  length  = 32
  special = false
}

resource "google_sql_database_instance" "teetime" {
  count            = var.enable_cloud_sql ? 1 : 0
  name             = "${local.service_name}-db"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier      = var.cloud_sql_tier
    disk_size = var.cloud_sql_disk_size
    disk_type = "PD_SSD"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled = true
    }
  }

  deletion_protection = true

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "teetime" {
  count    = var.enable_cloud_sql ? 1 : 0
  name     = local.service_name
  instance = google_sql_database_instance.teetime[0].name
}

resource "google_sql_user" "teetime" {
  count    = var.enable_cloud_sql ? 1 : 0
  name     = local.service_name
  instance = google_sql_database_instance.teetime[0].name
  password = random_password.db_password[0].result
}

resource "google_project_iam_member" "cloud_run_sql_client" {
  count   = var.enable_cloud_sql ? 1 : 0
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}
