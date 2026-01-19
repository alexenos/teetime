from enum import Enum

from pydantic_settings import BaseSettings


class WaitMode(str, Enum):
    """
    Wait strategy mode for Selenium operations.

    FIXED: Use fixed sleep durations (current behavior, most reliable)
    EVENT_DRIVEN: Use WebDriverWait only, no fixed sleeps (fastest, less reliable)
    HYBRID: Use WebDriverWait + small buffer sleep (balanced approach)
    """

    FIXED = "fixed"
    EVENT_DRIVEN = "event_driven"
    HYBRID = "hybrid"


class Settings(BaseSettings):
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_channel: str = "whatsapp"  # "sms" or "whatsapp"

    gemini_api_key: str = ""

    walden_member_number: str = ""
    walden_password: str = ""
    walden_base_url: str = "https://www.waldengolf.com"

    user_phone_number: str = ""

    database_url: str = "sqlite+aiosqlite:///./teetime.db"

    timezone: str = "America/Chicago"
    booking_open_hour: int = 6
    booking_open_minute: int = 30
    days_in_advance: int = 7
    max_tee_times_per_day: int = 2

    scheduler_api_key: str = ""
    scheduler_service_account: str = ""
    oidc_audience: str = ""  # Expected OIDC audience (Cloud Run service URL)

    # Logging configuration
    log_level: str = "INFO"  # Set to "DEBUG" to see BOOKING_DEBUG messages in GCP Cloud Logs

    # Wait strategy for Selenium operations (fixed, event_driven, hybrid)
    wait_mode: WaitMode = WaitMode.FIXED

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
