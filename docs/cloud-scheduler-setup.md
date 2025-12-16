# Cloud Scheduler Setup for TeeTime

This guide explains how to set up Google Cloud Scheduler to automatically execute scheduled tee time bookings.

## Overview

The TeeTime application includes a job endpoint that executes all due bookings. Cloud Scheduler calls this endpoint at 6:30am CT daily to trigger booking execution at the moment reservation windows open.

## Prerequisites

1. Google Cloud project with billing enabled
2. TeeTime application deployed to Cloud Run
3. Cloud Scheduler API enabled in your project

## Setup Steps

### 1. Generate a Scheduler API Key

Generate a secure random API key that will be used to authenticate Cloud Scheduler requests:

```bash
openssl rand -hex 32
```

Save this key - you'll need it for both Cloud Run and Cloud Scheduler configuration.

### 2. Configure Cloud Run Environment

Add the `SCHEDULER_API_KEY` environment variable to your Cloud Run service:

```bash
gcloud run services update teetime \
  --update-env-vars SCHEDULER_API_KEY=your_generated_api_key \
  --region us-central1
```

Or set it in your Cloud Run deployment configuration.

### 3. Create Cloud Scheduler Job

Create a Cloud Scheduler job that calls the execute-due-bookings endpoint:

```bash
gcloud scheduler jobs create http execute-tee-time-bookings \
  --location us-central1 \
  --schedule "30 6 * * *" \
  --time-zone "America/Chicago" \
  --uri "https://your-cloud-run-url/jobs/execute-due-bookings" \
  --http-method POST \
  --headers "X-Scheduler-API-Key=your_generated_api_key" \
  --attempt-deadline 540s \
  --retry-count 3 \
  --min-backoff 30s \
  --max-backoff 120s
```

Replace `your-cloud-run-url` with your actual Cloud Run service URL.

### Configuration Options

The schedule `30 6 * * *` runs at 6:30am daily in the specified timezone (America/Chicago = CT).

You can adjust the schedule using standard cron syntax:
- `30 6 * * *` - Every day at 6:30am
- `30 6 * * 1-5` - Weekdays only at 6:30am
- `*/30 6-7 * * *` - Every 30 minutes between 6am-7am

### 4. Test the Setup

You can manually trigger the job to test:

```bash
gcloud scheduler jobs run execute-tee-time-bookings --location us-central1
```

Or call the endpoint directly:

```bash
curl -X POST https://your-cloud-run-url/jobs/execute-due-bookings \
  -H "X-Scheduler-API-Key: your_generated_api_key"
```

## Monitoring

### View Job Execution History

```bash
gcloud scheduler jobs describe execute-tee-time-bookings --location us-central1
```

### View Cloud Run Logs

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=teetime" --limit 50
```

## Endpoint Response

The `/jobs/execute-due-bookings` endpoint returns a JSON response with execution results:

```json
{
  "executed_at": "2024-01-15T06:30:00-06:00",
  "total_due": 3,
  "executed": 3,
  "succeeded": 2,
  "failed": 1,
  "results": [
    {
      "booking_id": "abc123",
      "status": "success",
      "requested_date": "2024-01-22",
      "requested_time": "08:00:00"
    },
    {
      "booking_id": "def456",
      "status": "failed",
      "error": "No available slots",
      "requested_date": "2024-01-22",
      "requested_time": "09:00:00"
    }
  ]
}
```

## Security Notes

1. The API key should be kept secret and rotated periodically
2. Cloud Scheduler uses HTTPS to call the endpoint securely
3. The endpoint validates the API key before processing any bookings
4. Consider using Cloud Run's built-in authentication with service accounts for additional security

## Troubleshooting

### Job Not Running

1. Check that Cloud Scheduler API is enabled
2. Verify the schedule timezone is correct
3. Check Cloud Scheduler job logs for errors

### Authentication Failures

1. Verify the API key matches between Cloud Scheduler and Cloud Run
2. Check that the `SCHEDULER_API_KEY` environment variable is set in Cloud Run
3. Ensure the header name is exactly `X-Scheduler-API-Key`

### Bookings Not Executing

1. Check that bookings have `SCHEDULED` status
2. Verify `scheduled_execution_time` is in the past
3. Review Cloud Run logs for execution errors
