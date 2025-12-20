output "cloud_run_url" {
  description = "URL of the Cloud Run service"
  value       = google_cloud_run_v2_service.teetime.uri
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository URL"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.teetime.repository_id}"
}

output "cloud_run_service_account" {
  description = "Cloud Run service account email"
  value       = google_service_account.cloud_run.email
}

output "cloud_build_service_account" {
  description = "Cloud Build service account email"
  value       = google_service_account.cloud_build.email
}

output "scheduler_service_account" {
  description = "Cloud Scheduler service account email"
  value       = google_service_account.scheduler.email
}

output "twilio_webhook_url" {
  description = "URL to configure in Twilio for SMS webhook"
  value       = "${google_cloud_run_v2_service.teetime.uri}/webhooks/twilio/sms"
}

output "secrets_to_populate" {
  description = "List of secrets that need values added via gcloud"
  value       = local.secrets
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL instance connection name (if enabled)"
  value       = var.enable_cloud_sql ? google_sql_database_instance.teetime[0].connection_name : null
}
