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

## GCP Deployment

### Prerequisites

1. Google Cloud account with billing enabled
2. GCP project created (e.g., "teetime")
3. `gcloud` CLI installed and authenticated

### Quick Deploy

Run the deployment script:

```bash
./deploy.sh teetime us-central1
```

This will enable required APIs, build the container, and deploy to Cloud Run.

### Manual Deployment Steps

1. Enable required APIs:
   ```bash
   gcloud services enable cloudbuild.googleapis.com run.googleapis.com containerregistry.googleapis.com secretmanager.googleapis.com
   ```

2. Build and push the container:
   ```bash
   gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/teetime .
   ```

3. Deploy to Cloud Run:
   ```bash
   gcloud run deploy teetime \
       --image gcr.io/YOUR_PROJECT_ID/teetime \
       --region us-central1 \
       --platform managed \
       --allow-unauthenticated \
       --memory 1Gi
   ```

### Configure Secrets

Store sensitive credentials in Secret Manager:

```bash
# Create secrets
echo -n "your_twilio_sid" | gcloud secrets create TWILIO_ACCOUNT_SID --data-file=-
echo -n "your_twilio_token" | gcloud secrets create TWILIO_AUTH_TOKEN --data-file=-
echo -n "your_twilio_phone" | gcloud secrets create TWILIO_PHONE_NUMBER --data-file=-
echo -n "your_gemini_key" | gcloud secrets create GEMINI_API_KEY --data-file=-
echo -n "your_walden_member" | gcloud secrets create WALDEN_MEMBER_NUMBER --data-file=-
echo -n "your_walden_password" | gcloud secrets create WALDEN_PASSWORD --data-file=-
echo -n "your_scheduler_key" | gcloud secrets create SCHEDULER_API_KEY --data-file=-

# Grant Cloud Run access to secrets
gcloud run services update teetime --region us-central1 \
    --set-secrets="TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest,TWILIO_PHONE_NUMBER=TWILIO_PHONE_NUMBER:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,WALDEN_MEMBER_NUMBER=WALDEN_MEMBER_NUMBER:latest,WALDEN_PASSWORD=WALDEN_PASSWORD:latest,SCHEDULER_API_KEY=SCHEDULER_API_KEY:latest"
```

### Configure Twilio Webhook

1. Get your Cloud Run service URL:
   ```bash
   gcloud run services describe teetime --region us-central1 --format="value(status.url)"
   ```

2. In Twilio Console, set the webhook URL for your phone number to:
   ```
   https://YOUR_SERVICE_URL/webhooks/twilio/sms
   ```

### Set Up Cloud Scheduler

Create a Cloud Scheduler job to execute due bookings every minute:

```bash
gcloud scheduler jobs create http teetime-execute-bookings \
    --location us-central1 \
    --schedule "* * * * *" \
    --uri "https://YOUR_SERVICE_URL/jobs/execute-due-bookings" \
    --http-method POST \
    --headers "X-Scheduler-API-Key=YOUR_SCHEDULER_API_KEY"
```

### Database Setup (Production)

For production, use Cloud SQL instead of SQLite:

1. Create a Cloud SQL PostgreSQL instance
2. Set the `DATABASE_URL` environment variable:
   ```
   DATABASE_URL=postgresql+asyncpg://user:password@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
   ```

## License

Private - All rights reserved
