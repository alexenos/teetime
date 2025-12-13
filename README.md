# TeeTime - Golf Reservation Assistant

An LLM-powered application that helps reserve golf tee times at Walden Golf Club via SMS.

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

## Deployment

The application is designed to run on Google Cloud Run with Cloud Scheduler for timed booking execution.

## License

Private - All rights reserved
