from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import settings


class SMSService:
    def __init__(self) -> None:
        self._client: Client | None = None
        self._validator: RequestValidator | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        return self._client

    @property
    def validator(self) -> RequestValidator:
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

    async def send_sms(self, to_number: str, message: str) -> str | None:
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            print(f"[SMS Mock] To: {to_number}, Message: {message}")
            return "mock_sid"

        try:
            result = self.client.messages.create(
                body=message,
                from_=settings.twilio_phone_number,
                to=to_number,
            )
            return result.sid
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return None

    async def send_booking_confirmation(self, to_number: str, booking_details: str) -> str | None:
        message = f"Tee time booking confirmed! {booking_details}"
        return await self.send_sms(to_number, message)

    async def send_booking_failure(
        self, to_number: str, reason: str, alternatives: str | None = None
    ) -> str | None:
        message = f"Unable to book tee time: {reason}"
        if alternatives:
            message += f"\n\nAlternatives available: {alternatives}"
        return await self.send_sms(to_number, message)

    async def send_weekly_prompt(self, to_number: str) -> str | None:
        message = (
            "Hi! What tee times would you like this week? "
            "Reply with something like 'Saturday 8am, 4 players' or 'Same as last week'."
        )
        return await self.send_sms(to_number, message)


sms_service = SMSService()
