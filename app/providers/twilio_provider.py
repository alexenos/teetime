from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import settings
from app.providers.sms_base import SMSProvider, SMSResult


class TwilioSMSProvider(SMSProvider):
    """Twilio implementation of the SMS provider interface.

    Supports both SMS and WhatsApp channels via the twilio_channel setting.
    WhatsApp uses the same Twilio Messages API but with 'whatsapp:' prefix on phone numbers.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._validator: RequestValidator | None = None

    @property
    def client(self) -> Client:
        """Lazily initialize and return the Twilio client."""
        if self._client is None:
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        return self._client

    @property
    def validator(self) -> RequestValidator:
        """Lazily initialize and return the Twilio request validator."""
        if self._validator is None:
            self._validator = RequestValidator(settings.twilio_auth_token)
        return self._validator

    @property
    def is_whatsapp(self) -> bool:
        """Check if WhatsApp channel is configured."""
        return settings.twilio_channel.lower() == "whatsapp"

    def _format_phone_for_channel(self, phone_number: str) -> str:
        """
        Format a phone number for the configured channel.

        For WhatsApp, adds 'whatsapp:' prefix if not already present.
        For SMS, returns the number as-is (E.164 format).

        Args:
            phone_number: Phone number in E.164 format (e.g., +15551234567)

        Returns:
            Formatted phone number for the channel.
        """
        normalized = self.normalize_phone_number(phone_number)
        if self.is_whatsapp and not normalized.startswith("whatsapp:"):
            return f"whatsapp:{normalized}"
        return normalized

    @staticmethod
    def normalize_phone_number(phone_number: str) -> str:
        """
        Normalize a phone number by stripping the 'whatsapp:' prefix if present.

        This ensures consistent phone number format for internal use (sessions, DB).

        Args:
            phone_number: Phone number that may have 'whatsapp:' prefix.

        Returns:
            Phone number in E.164 format without channel prefix.
        """
        if phone_number.startswith("whatsapp:"):
            return phone_number[9:]  # len("whatsapp:") == 9
        return phone_number

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """
        Validate a Twilio webhook request signature.

        Security behavior:
        - If twilio_auth_token is NOT set (dev mode): Always returns True (skip validation)
        - If twilio_auth_token IS set (production): Requires valid signature header
          - Missing signature header -> returns False (reject request)
          - Invalid signature -> returns False (reject request)
          - Valid signature -> returns True (allow request)

        Args:
            url: The full URL of the webhook request.
            params: The form parameters from the request.
            signature: The X-Twilio-Signature header value (may be None).

        Returns:
            True if the request is valid, False otherwise.
        """
        if not settings.twilio_auth_token:
            return True
        if not signature:
            return False
        return self.validator.validate(url, params, signature)

    async def send_sms(self, to_number: str, message: str) -> SMSResult:
        """
        Send a message via Twilio (SMS or WhatsApp based on channel setting).

        Args:
            to_number: The recipient's phone number in E.164 format.
            message: The message content to send.

        Returns:
            SMSResult with success status and message SID or error message.
        """
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            channel = "WhatsApp" if self.is_whatsapp else "SMS"
            print(f"[{channel} Mock] To: {to_number}, Message: {message}")
            return SMSResult(success=True, message_sid="mock_sid")

        try:
            from_number = self._format_phone_for_channel(settings.twilio_phone_number)
            to_formatted = self._format_phone_for_channel(to_number)

            result = self.client.messages.create(
                body=message,
                from_=from_number,
                to=to_formatted,
            )
            return SMSResult(success=True, message_sid=result.sid)
        except Exception as e:
            channel = "WhatsApp" if self.is_whatsapp else "SMS"
            print(f"Error sending {channel}: {e}")
            return SMSResult(success=False, error_message=str(e))


class MockSMSProvider(SMSProvider):
    """Mock SMS provider for testing and development."""

    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def validate_request(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        """Always return True for mock provider."""
        return True

    async def send_sms(self, to_number: str, message: str) -> SMSResult:
        """Record the message and return a mock success result."""
        self.sent_messages.append({"to": to_number, "message": message})
        print(f"[SMS Mock] To: {to_number}, Message: {message}")
        return SMSResult(success=True, message_sid=f"mock_sid_{len(self.sent_messages)}")
