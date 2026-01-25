"""
Tests for SMSService in app/services/sms_service.py.

These tests verify the Twilio SMS integration, including request validation
and message sending functionality.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.providers.twilio_provider import MockSMSProvider, TwilioSMSProvider
from app.services.sms_service import SMSService


@pytest.fixture
def sms_service() -> SMSService:
    """Create a fresh SMSService instance for each test."""
    return SMSService()


@pytest.fixture
def mock_provider() -> MockSMSProvider:
    """Create a MockSMSProvider for testing."""
    return MockSMSProvider()


class TestSMSServiceValidateRequest:
    """Tests for the validate_request method."""

    def test_validate_request_with_mock_provider(self, sms_service: SMSService) -> None:
        """Test that validation passes with mock provider (dev mode)."""
        mock_provider = MockSMSProvider()
        sms_service.set_provider(mock_provider)

        result = sms_service.validate_request(
            url="http://example.com/webhook",
            params={"Body": "Hello"},
            signature=None,
        )

        assert result is True

    def test_validate_request_no_auth_token(self) -> None:
        """Test that TwilioSMSProvider validation passes when no auth token is configured."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_auth_token = ""

            provider = TwilioSMSProvider()
            result = provider.validate_request(
                url="http://example.com/webhook",
                params={"Body": "Hello"},
                signature=None,
            )

            assert result is True

    def test_validate_request_missing_signature(self) -> None:
        """Test that validation fails when signature is missing but auth token is set."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            provider = TwilioSMSProvider()
            result = provider.validate_request(
                url="http://example.com/webhook",
                params={"Body": "Hello"},
                signature=None,
            )

            assert result is False

    def test_validate_request_valid_signature(self) -> None:
        """Test that validation passes with valid signature."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            provider = TwilioSMSProvider()
            with patch.object(provider, "_validator") as mock_validator:
                mock_validator.validate.return_value = True

                result = provider.validate_request(
                    url="http://example.com/webhook",
                    params={"Body": "Hello"},
                    signature="valid_signature",
                )

                assert result is True

    def test_validate_request_invalid_signature(self) -> None:
        """Test that validation fails with invalid signature."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            provider = TwilioSMSProvider()
            with patch.object(provider, "_validator") as mock_validator:
                mock_validator.validate.return_value = False

                result = provider.validate_request(
                    url="http://example.com/webhook",
                    params={"Body": "Hello"},
                    signature="invalid_signature",
                )

                assert result is False


class TestSMSServiceSendSMS:
    """Tests for the send_sms method."""

    @pytest.mark.asyncio
    async def test_send_sms_with_mock_provider(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test that send_sms works with mock provider."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_sms("+15551234567", "Hello!")

        assert result is not None
        assert result.startswith("mock_sid")
        assert len(mock_provider.sent_messages) == 1
        assert mock_provider.sent_messages[0]["to"] == "+15551234567"
        assert mock_provider.sent_messages[0]["message"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_sms_no_credentials(self) -> None:
        """Test that TwilioSMSProvider returns mock result when no credentials are configured."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""

            provider = TwilioSMSProvider()
            result = await provider.send_sms("+15551234567", "Hello!")

            assert result.success is True
            assert result.message_sid == "mock_sid"

    @pytest.mark.asyncio
    async def test_send_sms_success(self) -> None:
        """Test successful SMS sending via Twilio."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"
            mock_settings.twilio_phone_number = "+15559999999"

            provider = TwilioSMSProvider()
            mock_client = MagicMock()
            mock_message = MagicMock()
            mock_message.sid = "SM123456"
            mock_client.messages.create.return_value = mock_message

            with patch.object(provider, "_client", mock_client):
                result = await provider.send_sms("+15551234567", "Hello!")

            assert result.success is True
            assert result.message_sid == "SM123456"
            mock_client.messages.create.assert_called_once_with(
                body="Hello!",
                from_="+15559999999",
                to="+15551234567",
            )

    @pytest.mark.asyncio
    async def test_send_sms_error(self) -> None:
        """Test SMS sending error handling."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"
            mock_settings.twilio_phone_number = "+15559999999"

            provider = TwilioSMSProvider()
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = Exception("Twilio error")

            with patch.object(provider, "_client", mock_client):
                result = await provider.send_sms("+15551234567", "Hello!")

            assert result.success is False
            assert result.error_message == "Twilio error"


class TestSMSServiceBookingNotifications:
    """Tests for booking notification methods."""

    @pytest.mark.asyncio
    async def test_send_booking_confirmation(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test sending booking confirmation."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_booking_confirmation(
            "+15551234567",
            "Saturday, December 20 at 08:00 AM for 4 players",
        )

        assert result is not None
        assert result.startswith("mock_sid")
        assert len(mock_provider.sent_messages) == 1
        assert mock_provider.sent_messages[0]["to"] == "+15551234567"
        assert "confirmed" in mock_provider.sent_messages[0]["message"].lower()
        assert "Saturday, December 20" in mock_provider.sent_messages[0]["message"]

    @pytest.mark.asyncio
    async def test_send_booking_failure(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test sending booking failure notification."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_booking_failure(
            "+15551234567",
            "Time slot not available",
        )

        assert result is not None
        assert result.startswith("mock_sid")
        assert len(mock_provider.sent_messages) == 1
        assert mock_provider.sent_messages[0]["to"] == "+15551234567"
        assert "unable to book" in mock_provider.sent_messages[0]["message"].lower()
        assert "Time slot not available" in mock_provider.sent_messages[0]["message"]

    @pytest.mark.asyncio
    async def test_send_booking_failure_with_alternatives(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test sending booking failure with alternatives."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_booking_failure(
            "+15551234567",
            "Time slot not available",
            alternatives="8:08 AM, 8:16 AM",
        )

        assert result is not None
        assert "alternatives" in mock_provider.sent_messages[0]["message"].lower()
        assert "8:08 AM, 8:16 AM" in mock_provider.sent_messages[0]["message"]

    @pytest.mark.asyncio
    async def test_send_booking_failure_with_booking_details(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test sending booking failure with specific booking details."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_booking_failure(
            "+15551234567",
            "No time slots with 4 available spots found",
            booking_details="Sunday, February 01 at 08:58 AM for 4 players",
        )

        assert result is not None
        message = mock_provider.sent_messages[0]["message"]
        assert "Sunday, February 01 at 08:58 AM for 4 players" in message
        assert "No time slots with 4 available spots found" in message

    @pytest.mark.asyncio
    async def test_send_weekly_prompt(
        self, sms_service: SMSService, mock_provider: MockSMSProvider
    ) -> None:
        """Test sending weekly prompt."""
        sms_service.set_provider(mock_provider)

        result = await sms_service.send_weekly_prompt("+15551234567")

        assert result is not None
        assert result.startswith("mock_sid")
        assert len(mock_provider.sent_messages) == 1
        assert mock_provider.sent_messages[0]["to"] == "+15551234567"
        assert "tee times" in mock_provider.sent_messages[0]["message"].lower()


class TestSMSServiceProperties:
    """Tests for SMSService provider property."""

    def test_provider_property_cached(self, sms_service: SMSService) -> None:
        """Test that provider property is cached."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""

            provider1 = sms_service.provider
            provider2 = sms_service.provider

            assert provider1 is provider2

    def test_provider_uses_twilio_when_configured(self) -> None:
        """Test that TwilioSMSProvider is used when credentials are configured."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"

            service = SMSService()
            provider = service.provider

            assert isinstance(provider, TwilioSMSProvider)

    def test_provider_uses_mock_when_not_configured(self) -> None:
        """Test that MockSMSProvider is used when credentials are not configured."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""

            service = SMSService()
            provider = service.provider

            assert isinstance(provider, MockSMSProvider)


class TestTwilioSMSProviderProperties:
    """Tests for TwilioSMSProvider properties."""

    def test_client_property_cached(self) -> None:
        """Test that client property is cached."""
        with patch("app.providers.twilio_provider.Client") as mock_client_class:
            mock_client_class.return_value = MagicMock()

            provider = TwilioSMSProvider()
            client1 = provider.client
            client2 = provider.client

            assert client1 is client2
            mock_client_class.assert_called_once()

    def test_validator_property_cached(self) -> None:
        """Test that validator property is cached."""
        with patch("app.providers.twilio_provider.RequestValidator") as mock_validator_class:
            mock_validator_class.return_value = MagicMock()

            provider = TwilioSMSProvider()
            validator1 = provider.validator
            validator2 = provider.validator

            assert validator1 is validator2
            mock_validator_class.assert_called_once()


class TestTwilioSMSProviderWhatsApp:
    """Tests for WhatsApp channel support in TwilioSMSProvider."""

    def test_normalize_phone_number_strips_whatsapp_prefix(self) -> None:
        """Test that normalize_phone_number strips the whatsapp: prefix."""
        result = TwilioSMSProvider.normalize_phone_number("whatsapp:+15551234567")
        assert result == "+15551234567"

    def test_normalize_phone_number_preserves_plain_number(self) -> None:
        """Test that normalize_phone_number preserves numbers without prefix."""
        result = TwilioSMSProvider.normalize_phone_number("+15551234567")
        assert result == "+15551234567"

    def test_is_whatsapp_true_when_configured(self) -> None:
        """Test that is_whatsapp returns True when channel is whatsapp."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "whatsapp"

            provider = TwilioSMSProvider()
            assert provider.is_whatsapp is True

    def test_is_whatsapp_false_when_sms(self) -> None:
        """Test that is_whatsapp returns False when channel is sms."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "sms"

            provider = TwilioSMSProvider()
            assert provider.is_whatsapp is False

    def test_is_whatsapp_case_insensitive(self) -> None:
        """Test that is_whatsapp is case insensitive."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "WhatsApp"

            provider = TwilioSMSProvider()
            assert provider.is_whatsapp is True

    def test_format_phone_for_whatsapp_channel(self) -> None:
        """Test that phone numbers are formatted with whatsapp: prefix for WhatsApp channel."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "whatsapp"

            provider = TwilioSMSProvider()
            result = provider._format_phone_for_channel("+15551234567")
            assert result == "whatsapp:+15551234567"

    def test_format_phone_for_sms_channel(self) -> None:
        """Test that phone numbers are not modified for SMS channel."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "sms"

            provider = TwilioSMSProvider()
            result = provider._format_phone_for_channel("+15551234567")
            assert result == "+15551234567"

    def test_format_phone_avoids_double_prefix(self) -> None:
        """Test that format doesn't double-prefix numbers already with whatsapp:."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_channel = "whatsapp"

            provider = TwilioSMSProvider()
            result = provider._format_phone_for_channel("whatsapp:+15551234567")
            assert result == "whatsapp:+15551234567"

    @pytest.mark.asyncio
    async def test_send_sms_uses_whatsapp_format(self) -> None:
        """Test that send_sms uses whatsapp: prefix when channel is whatsapp."""
        with patch("app.providers.twilio_provider.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"
            mock_settings.twilio_phone_number = "+15559999999"
            mock_settings.twilio_channel = "whatsapp"

            provider = TwilioSMSProvider()
            mock_client = MagicMock()
            mock_message = MagicMock()
            mock_message.sid = "SM123456"
            mock_client.messages.create.return_value = mock_message

            with patch.object(provider, "_client", mock_client):
                result = await provider.send_sms("+15551234567", "Hello!")

            assert result.success is True
            mock_client.messages.create.assert_called_once_with(
                body="Hello!",
                from_="whatsapp:+15559999999",
                to="whatsapp:+15551234567",
            )
