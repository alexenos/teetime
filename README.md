# TeeTime - Golf Reservation Assistant

An LLM-powered application that helps reserve golf tee times at Northgate Country Club via SMS.

> **Note**: The booking platform is waldengolf.com (Walden Golf manages multiple clubs), but this application is specifically configured for Northgate Country Club.

## Features

- SMS-based interface using Twilio
- Natural language understanding via Google Gemini API
- Automated booking at reservation open time (6:30am CT, 7 days in advance)
- Confirmation and status notifications

## Architecture

- **Backend**: FastAPI on Google Cloud Run
- **SMS**: Twilio for inbound/outbound messaging
- **LLM**: Google Gemini API with function calling
- **Scheduling**: Cloud Run Jobs + Cloud Scheduler
- **Database**: Cloud SQL (Postgres) / SQLite for local dev

## Local Development

### Prerequisites

- Python 3.11+
- Poetry

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/alexenos/teetime.git
   cd teetime
   ```

2. Install dependencies:
   ```bash
   poetry install
   ```

3. Copy the example environment file and configure:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. Run the development server:
   ```bash
   poetry run uvicorn app.main:app --reload
   ```

5. The API will be available at http://localhost:8000

### Testing

```bash
poetry run pytest
```

### Linting

```bash
poetry run ruff check .
poetry run mypy app
```

## API Endpoints

- `GET /` - Service info
- `GET /health` - Health check
- `POST /webhooks/twilio/sms` - Twilio SMS webhook
- `GET /bookings` - List bookings
- `POST /bookings` - Create booking
- `GET /bookings/{id}` - Get booking details
- `DELETE /bookings/{id}` - Cancel booking
- `POST /bookings/{id}/execute` - Execute booking (for testing)

## Environment Variables

See `.env.example` for all configuration options.

## GCP Deployment (Terraform)

All GCP infrastructure is managed via Terraform in the `terraform/` directory.

### Prerequisites

1. Google Cloud account with billing enabled
2. GCP project created (e.g., "teetime")
3. `gcloud` CLI installed and authenticated
4. Terraform >= 1.0 installed
5. GitHub repository connected to Cloud Build (one-time setup)

### Initial Setup

1. Create a GCS bucket for Terraform state:
   ```bash
   PROJECT_ID="teetime"
   gsutil mb -p $PROJECT_ID gs://${PROJECT_ID}-terraform-state
   gsutil versioning set on gs://${PROJECT_ID}-terraform-state
   ```

2. Connect GitHub to Cloud Build (one-time, via console):
   - Go to https://console.cloud.google.com/cloud-build/triggers
   - Click "Connect Repository" and follow the OAuth flow to connect your GitHub account
   - Select the `alexenos/teetime` repository

3. Configure Terraform variables:
   ```bash
   cd terraform
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your project settings
   ```

4. Initialize and apply Terraform:
   ```bash
   terraform init -backend-config="bucket=${PROJECT_ID}-terraform-state"
   terraform plan
   terraform apply
   ```

### Add Secret Values

Terraform creates the secret containers but not the values (for security). Add values via gcloud:

```bash
echo -n "your_twilio_sid" | gcloud secrets versions add TWILIO_ACCOUNT_SID --data-file=-
echo -n "your_twilio_token" | gcloud secrets versions add TWILIO_AUTH_TOKEN --data-file=-
echo -n "+1234567890" | gcloud secrets versions add TWILIO_PHONE_NUMBER --data-file=-
echo -n "your_gemini_key" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
echo -n "your_member_number" | gcloud secrets versions add WALDEN_MEMBER_NUMBER --data-file=-
echo -n "your_password" | gcloud secrets versions add WALDEN_PASSWORD --data-file=-
echo -n "your_scheduler_key" | gcloud secrets versions add SCHEDULER_API_KEY --data-file=-
echo -n "+1234567890" | gcloud secrets versions add USER_PHONE_NUMBER --data-file=-
```

### First Deployment

After Terraform creates the infrastructure, trigger the first build:

```bash
# Push to main to trigger auto-deployment, or manually trigger:
gcloud builds submit --config=cloudbuild.yaml .
```

### Configure Twilio Webhook

Get the Cloud Run URL from Terraform output and configure in Twilio:

```bash
cd terraform
terraform output cloud_run_url
# Configure this URL + /webhooks/twilio/sms in Twilio Console
```

### Optional: Enable Cloud SQL

To use PostgreSQL instead of SQLite, set `enable_cloud_sql = true` in terraform.tfvars:

```hcl
enable_cloud_sql    = true
cloud_sql_tier      = "db-f1-micro"
cloud_sql_disk_size = 10
```

Then run `terraform apply`. This adds ~$7-10/month cost.

### Terraform Resources Created

The Terraform configuration manages:
- Cloud Run service with auto-scaling
- Artifact Registry repository
- Secret Manager secrets (containers only)
- Cloud Scheduler job for booking execution
- IAM service accounts and bindings
- Cloud SQL PostgreSQL (optional)

## License

Private - All rights reserved
