from app.config import settings
from app.providers.sms_base import SMSProvider
from app.providers.twilio_provider import MockSMSProvider, TwilioSMSProvider


class SMSService:
    """
    Service for sending SMS messages.

    This service wraps an SMS provider (Twilio or Mock) and provides
    a consistent interface for sending messages throughout the application.
    """

    def __init__(self) -> None:
        self._provider: SMSProvider | None = None

    @property
    def provider(self) -> SMSProvider:
        """
        Lazily initialize and return the SMS provider.

        Uses TwilioSMSProvider if credentials are configured,
        otherwise falls back to MockSMSProvider for development.
        """
        if self._provider is None:
            if settings.twilio_account_sid and settings.twilio_auth_token:
                self._provider = TwilioSMSProvider()
            else:
                self._provider = MockSMSProvider()
        return self._provider

    def set_provider(self, provider: SMSProvider) -> None:
        """Set a custom SMS provider (useful for testing)."""
        self._provider = provider

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """
        Validate a webhook request signature.

        Delegates to the underlying SMS provider's validation logic.

        Args:
            url: The full URL of the webhook request.
            params: The form parameters from the request.
            signature: The signature header value (may be None).

        Returns:
            True if the request is valid, False otherwise.
        """
        return self.provider.validate_request(url, params, signature)

    async def send_sms(self, to_number: str, message: str) -> str | None:
        """
        Send an SMS message.

        Args:
            to_number: The recipient's phone number.
            message: The message content.

        Returns:
            The message SID if successful, None otherwise.
        """
        result = await self.provider.send_sms(to_number, message)
        return result.message_sid if result.success else None

    async def send_booking_confirmation(self, to_number: str, booking_details: str) -> str | None:
        """Send a booking confirmation SMS."""
        result = await self.provider.send_booking_confirmation(to_number, booking_details)
        return result.message_sid if result.success else None

    async def send_booking_failure(
        self,
        to_number: str,
        reason: str,
        alternatives: str | None = None,
        booking_details: str | None = None,
    ) -> str | None:
        """Send a booking failure notification SMS.

        Args:
            to_number: The recipient's phone number.
            reason: The reason for the booking failure.
            alternatives: Optional alternative time slots available.
            booking_details: Optional details about the specific booking that failed
                           (e.g., "Sunday, February 01 at 08:58 AM for 4 players").
        """
        result = await self.provider.send_booking_failure(
            to_number, reason, alternatives, booking_details
        )
        return result.message_sid if result.success else None

    async def send_weekly_prompt(self, to_number: str) -> str | None:
        """Send a weekly tee time prompt SMS."""
        result = await self.provider.send_weekly_prompt(to_number)
        return result.message_sid if result.success else None


sms_service = SMSService()
