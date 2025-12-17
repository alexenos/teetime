from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import settings
from app.providers.sms_base import SMSProvider, SMSResult


class TwilioSMSProvider(SMSProvider):
    """Twilio implementation of the SMS provider interface."""

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

    def validate_request(self, url: str, params: dict, signature: str | None) -> bool:
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
        Send an SMS message via Twilio.

        Args:
            to_number: The recipient's phone number in E.164 format.
            message: The message content to send.

        Returns:
            SMSResult with success status and message SID or error message.
        """
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            print(f"[SMS Mock] To: {to_number}, Message: {message}")
            return SMSResult(success=True, message_sid="mock_sid")

        try:
            result = self.client.messages.create(
                body=message,
                from_=settings.twilio_phone_number,
                to=to_number,
            )
            return SMSResult(success=True, message_sid=result.sid)
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return SMSResult(success=False, error_message=str(e))


class MockSMSProvider(SMSProvider):
    """Mock SMS provider for testing and development."""

    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def validate_request(self, url: str, params: dict, signature: str | None) -> bool:
        """Always return True for mock provider."""
        return True

    async def send_sms(self, to_number: str, message: str) -> SMSResult:
        """Record the message and return a mock success result."""
        self.sent_messages.append({"to": to_number, "message": message})
        print(f"[SMS Mock] To: {to_number}, Message: {message}")
        return SMSResult(success=True, message_sid=f"mock_sid_{len(self.sent_messages)}")
