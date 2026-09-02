from abc import ABC, abstractmethod
from dataclasses import dataclass


def split_message(message: str, limit: int) -> list[str]:
    """Split a message into chunks under a provider's length limit.

    Every chat platform caps message length (Discord at 2000 characters,
    Telegram at 4096) and rejects anything longer outright, so a long booking
    list has to be sent as several messages. Breaks on a newline where there is
    one inside the limit, since these messages are mostly line-per-booking.
    """
    if len(message) <= limit:
        return [message]
    chunks = []
    remaining = message
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


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
    async def send_sms(
        self, to_number: str, message: str, origin_channel_id: str | None = None
    ) -> SMSResult:
        """
        Send an SMS message.

        Args:
            to_number: The recipient's phone number.
            message: The message content.
            origin_channel_id: Where the conversation this message belongs to
                started, for providers that have a concept of channels. Discord
                uses it to reply in that channel; SMS providers ignore it.

        Returns:
            SMSResult with success status and message SID or error.
        """
        pass

    async def send_booking_confirmation(
        self, to_number: str, booking_details: str, origin_channel_id: str | None = None
    ) -> SMSResult:
        """Send a booking confirmation SMS."""
        message = f"Tee time booking confirmed! {booking_details}"
        return await self.send_sms(to_number, message, origin_channel_id)

    async def send_booking_failure(
        self,
        to_number: str,
        reason: str,
        alternatives: str | None = None,
        booking_details: str | None = None,
        origin_channel_id: str | None = None,
    ) -> SMSResult:
        """Send a booking failure notification SMS.

        Args:
            to_number: The recipient's phone number.
            reason: The reason for the booking failure.
            alternatives: Optional alternative time slots available.
            booking_details: Optional details about the specific booking that failed
                           (e.g., "Sunday, February 01 at 08:58 AM for 4 players").
            origin_channel_id: Channel the booking was requested in, so the
                failure replies there instead of a DM.
        """
        if booking_details:
            message = f"Unable to book tee time for {booking_details}: {reason}"
        else:
            message = f"Unable to book tee time: {reason}"
        if alternatives:
            message += f"\n\nAlternatives available: {alternatives}"
        return await self.send_sms(to_number, message, origin_channel_id)

    async def send_weekly_prompt(
        self, to_number: str, origin_channel_id: str | None = None
    ) -> SMSResult:
        """Send a weekly tee time prompt SMS."""
        message = (
            "Hi! What tee times would you like this week? "
            "Reply with something like 'Saturday 8am, 4 players' or 'Same as last week'."
        )
        return await self.send_sms(to_number, message, origin_channel_id)
