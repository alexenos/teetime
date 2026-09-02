from app.config import settings
from app.providers.discord_provider import DiscordProvider
from app.providers.sms_base import SMSProvider
from app.providers.telegram_provider import TelegramProvider
from app.providers.twilio_provider import MockSMSProvider, TwilioSMSProvider


class SMSService:
    """
    Service for sending messages over the user's messaging channel.

    More than one channel can be live at once - Telegram is reachable while
    Discord is still the configured default - so the provider is chosen per
    message rather than once for the process. Every notification carries the
    channel of the conversation that asked for it, recorded on the booking at
    creation time, and is answered over that same channel.

    A message with no channel of its own (the REST API, and rows written before
    the column existed) falls back to MESSAGING_CHANNEL, which is the behavior
    every message had before per-channel routing.
    """

    def __init__(self) -> None:
        self._provider: SMSProvider | None = None
        self._providers: dict[str, SMSProvider] = {}

    def _build_provider(self, channel: str) -> SMSProvider:
        """Construct the provider for a channel.

        A channel whose credentials are missing falls through to Twilio and then
        to Mock, rather than raising - the same cascade this method used when
        the channel was global, so a misconfigured channel still degrades
        instead of leaving a booking result undeliverable.
        """
        if channel == "discord" and settings.discord_bot_token:
            return DiscordProvider()
        if channel == "telegram" and settings.telegram_bot_token:
            return TelegramProvider()
        if settings.twilio_account_sid and settings.twilio_auth_token:
            return TwilioSMSProvider()
        return MockSMSProvider()

    def provider_for(self, channel: str | None) -> SMSProvider:
        """Return the provider for a conversation's channel.

        A provider explicitly installed via set_provider wins for every channel,
        so a test that stubs the provider keeps stubbing all of them.
        """
        if self._provider is not None:
            return self._provider
        resolved = (channel or settings.messaging_channel or "").strip()
        if resolved not in self._providers:
            self._providers[resolved] = self._build_provider(resolved)
        return self._providers[resolved]

    @property
    def provider(self) -> SMSProvider:
        """The provider for the configured default channel (MESSAGING_CHANNEL)."""
        return self.provider_for(None)

    def set_provider(self, provider: SMSProvider) -> None:
        """Set a custom SMS provider for every channel (useful for testing)."""
        self._provider = provider
        self._providers.clear()

    def validate_request(
        self,
        url: str,
        params: dict[str, str],
        signature: str | None,
        channel: str | None = None,
    ) -> bool:
        """
        Validate a webhook request signature.

        Delegates to the provider for the channel the request arrived on, which
        the caller names. A webhook route knows exactly which service is calling
        it, so it must not be answered by whichever provider MESSAGING_CHANNEL
        happens to select: Discord and Telegram both validate inbound through
        other means and return True here, so a Twilio request checked against
        either would have its signature waved through.

        Args:
            url: The full URL of the webhook request.
            params: The form parameters from the request.
            signature: The signature header value (may be None).
            channel: Channel the request arrived on. None falls back to
                MESSAGING_CHANNEL.

        Returns:
            True if the request is valid, False otherwise.
        """
        return self.provider_for(channel).validate_request(url, params, signature)

    async def send_sms(
        self,
        to_number: str,
        message: str,
        origin_channel_id: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        """
        Send an SMS message.

        Args:
            to_number: The recipient's phone number.
            message: The message content.
            origin_channel_id: Channel the conversation started in, for providers
                that support channels (Discord, Telegram). Ignored by SMS providers.
            channel: Messaging channel to send over. None falls back to
                MESSAGING_CHANNEL.

        Returns:
            The message SID if successful, None otherwise.
        """
        result = await self.provider_for(channel).send_sms(to_number, message, origin_channel_id)
        return result.message_sid if result.success else None

    async def send_booking_confirmation(
        self,
        to_number: str,
        booking_details: str,
        origin_channel_id: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        """Send a booking confirmation SMS."""
        result = await self.provider_for(channel).send_booking_confirmation(
            to_number, booking_details, origin_channel_id
        )
        return result.message_sid if result.success else None

    async def send_booking_failure(
        self,
        to_number: str,
        reason: str,
        alternatives: str | None = None,
        booking_details: str | None = None,
        origin_channel_id: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        """Send a booking failure notification SMS.

        Args:
            to_number: The recipient's phone number.
            reason: The reason for the booking failure.
            alternatives: Optional alternative time slots available.
            booking_details: Optional details about the specific booking that failed
                           (e.g., "Sunday, February 01 at 08:58 AM for 4 players").
            origin_channel_id: Channel the booking was requested in, so the
                failure replies there instead of a DM.
            channel: Messaging channel to send over. None falls back to
                MESSAGING_CHANNEL.
        """
        result = await self.provider_for(channel).send_booking_failure(
            to_number, reason, alternatives, booking_details, origin_channel_id
        )
        return result.message_sid if result.success else None

    async def send_weekly_prompt(
        self,
        to_number: str,
        origin_channel_id: str | None = None,
        channel: str | None = None,
    ) -> str | None:
        """Send a weekly tee time prompt SMS."""
        result = await self.provider_for(channel).send_weekly_prompt(to_number, origin_channel_id)
        return result.message_sid if result.success else None


sms_service = SMSService()
