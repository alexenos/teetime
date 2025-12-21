variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "github_owner" {
  description = "GitHub repository owner"
  type        = string
  default     = "alexenos"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "teetime"
}

variable "github_branch" {
  description = "GitHub branch to trigger deployments"
  type        = string
  default     = "main"
}

variable "cloud_run_memory" {
  description = "Memory allocation for Cloud Run service"
  type        = string
  default     = "1Gi"
}

variable "cloud_run_cpu" {
  description = "CPU allocation for Cloud Run service"
  type        = string
  default     = "1"
}

variable "cloud_run_max_instances" {
  description = "Maximum number of Cloud Run instances"
  type        = number
  default     = 10
}

variable "cloud_run_min_instances" {
  description = "Minimum number of Cloud Run instances (0 allows scale to zero)"
  type        = number
  default     = 0
}

variable "timezone" {
  description = "Timezone for the application"
  type        = string
  default     = "America/Chicago"
}

variable "booking_open_hour" {
  description = "Hour when booking opens (24-hour format)"
  type        = number
  default     = 6
}

variable "booking_open_minute" {
  description = "Minute when booking opens"
  type        = number
  default     = 30
}

variable "days_in_advance" {
  description = "Number of days in advance bookings can be made"
  type        = number
  default     = 7
}

variable "enable_cloud_sql" {
  description = "Enable Cloud SQL PostgreSQL instance (adds ~$7-10/month)"
  type        = bool
  default     = true
}

variable "cloud_sql_tier" {
  description = "Cloud SQL instance tier"
  type        = string
  default     = "db-f1-micro"
}

variable "cloud_sql_disk_size" {
  description = "Cloud SQL disk size in GB"
  type        = number
  default     = 10
}

variable "container_image" {
  description = "Container image to deploy (passed from Cloud Build)"
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "OIDC audience for Cloud Scheduler authentication. Set to the Cloud Run service URL after initial deployment. Leave empty to use API key auth."
  type        = string
  default     = "https://teetime-746475271596.us-central1.run.app"
}
