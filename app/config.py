from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
