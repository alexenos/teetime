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

    discord_bot_token: str = ""
    discord_user_id: str = ""  # Snowflake ID of the (single) user allowed to DM the bot
    # Snowflake ID of a shared channel (e.g. #general) to post outbound
    # notifications into. When set, booking confirmations/failures go to this
    # channel (mentioning the user) instead of a private DM, so the whole
    # conversation stays in one place. Leave empty to fall back to DMs.
    discord_channel_id: str = ""
    messaging_channel: str = "twilio"  # "twilio" or "discord"

    gemini_api_key: str = ""
    # Floating alias rather than a pinned version: a pinned model (gemini-2.0-flash)
    # was retired out from under us and every message silently mis-parsed.
    gemini_model: str = "gemini-flash-latest"

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
        # .env may carry keys this branch doesn't know about (e.g. settings
        # introduced on another branch); ignore them instead of crashing.
        extra = "ignore"


settings = Settings()
