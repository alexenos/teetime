###############################################################################
# A home for the Cloud Billing export, and read access for the cost Routine
# (issue #227, operations/routines/cost.md)
#
# Actual historical spend is readable programmatically only from the billing
# export to BigQuery. The Cloud Billing API exposes prices, not charges, and
# budgets give a threshold crossing, not a figure.
#
# The export itself is configured on the billing account, not the project, and
# there is no API or terraform resource for turning it on. A billing account
# admin does it once in the Console, pointing it at the dataset created here:
#
#   Billing > Billing export > BigQuery export > Detailed usage cost
#     project: this one    dataset: billing_export
#
# Until that happens the dataset is empty and costs nothing. The export is not
# retroactive: it starts from the day it is enabled.
###############################################################################

resource "google_bigquery_dataset" "billing_export" {
  dataset_id  = "billing_export"
  location    = "US"
  description = "Cloud Billing detailed usage cost export, read by the cost Routine (#227)."

  # The export cannot be re-run for past days. Losing the dataset loses the
  # history with no way to recover it, so a change that would replace or
  # destroy it must fail the apply instead.
  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.apis]
}

# The Routine sessions run as teetime-artifact-reader (see the ledger writer
# binding in main.tf for why that account is not itself managed here).
#
# jobUser is project-level because BigQuery bills and runs queries as jobs in a
# project; without it no query runs at all, whatever the dataset grants. It
# confers no read on any data.
resource "google_project_iam_member" "routine_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:teetime-artifact-reader@${var.project_id}.iam.gserviceaccount.com"
}

# Read on this one dataset only. dataViewer cannot write, delete or change the
# export.
resource "google_bigquery_dataset_iam_member" "routine_billing_reader" {
  dataset_id = google_bigquery_dataset.billing_export.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:teetime-artifact-reader@${var.project_id}.iam.gserviceaccount.com"
}
