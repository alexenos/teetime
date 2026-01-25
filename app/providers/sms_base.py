from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SMSResult:
    success: bool
    message_sid: str | None = None
    error_message: str | None = None


class SMSProvider(ABC):
    """Abstract base class for SMS providers."""

    @abstractmethod
    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """
        Validate an incoming webhook request signature.

        Args:
            url: The full URL of the webhook request.
            params: The form parameters from the request.
            signature: The signature header value (may be None).

        Returns:
            True if the request is valid, False otherwise.
        """
        pass

    @abstractmethod
    async def send_sms(self, to_number: str, message: str) -> SMSResult:
        """
        Send an SMS message.

        Args:
            to_number: The recipient's phone number.
            message: The message content.

        Returns:
            SMSResult with success status and message SID or error.
        """
        pass

    async def send_booking_confirmation(self, to_number: str, booking_details: str) -> SMSResult:
        """Send a booking confirmation SMS."""
        message = f"Tee time booking confirmed! {booking_details}"
        return await self.send_sms(to_number, message)

    async def send_booking_failure(
        self,
        to_number: str,
        reason: str,
        alternatives: str | None = None,
        booking_details: str | None = None,
    ) -> SMSResult:
        """Send a booking failure notification SMS.

        Args:
            to_number: The recipient's phone number.
            reason: The reason for the booking failure.
            alternatives: Optional alternative time slots available.
            booking_details: Optional details about the specific booking that failed
                           (e.g., "Sunday, February 01 at 08:58 AM for 4 players").
        """
        if booking_details:
            message = f"Unable to book tee time for {booking_details}: {reason}"
        else:
            message = f"Unable to book tee time: {reason}"
        if alternatives:
            message += f"\n\nAlternatives available: {alternatives}"
        return await self.send_sms(to_number, message)

    async def send_weekly_prompt(self, to_number: str) -> SMSResult:
        """Send a weekly tee time prompt SMS."""
        message = (
            "Hi! What tee times would you like this week? "
            "Reply with something like 'Saturday 8am, 4 players' or 'Same as last week'."
        )
        return await self.send_sms(to_number, message)
