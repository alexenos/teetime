"""
Tests for SMSService in app/services/sms_service.py.

These tests verify the Twilio SMS integration, including request validation
and message sending functionality.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.sms_service import SMSService


@pytest.fixture
def sms_service() -> SMSService:
    """Create a fresh SMSService instance for each test."""
    return SMSService()


class TestSMSServiceValidateRequest:
    """Tests for the validate_request method."""

    def test_validate_request_no_auth_token(self, sms_service: SMSService) -> None:
        """Test that validation passes when no auth token is configured (dev mode)."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_auth_token = ""

            result = sms_service.validate_request(
                url="http://example.com/webhook",
                params={"Body": "Hello"},
                signature=None,
            )

            assert result is True

    def test_validate_request_missing_signature(self, sms_service: SMSService) -> None:
        """Test that validation fails when signature is missing but auth token is set."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            result = sms_service.validate_request(
                url="http://example.com/webhook",
                params={"Body": "Hello"},
                signature=None,
            )

            assert result is False

    def test_validate_request_valid_signature(self, sms_service: SMSService) -> None:
        """Test that validation passes with valid signature."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            with patch.object(sms_service, "_validator") as mock_validator:
                mock_validator.validate.return_value = True

                result = sms_service.validate_request(
                    url="http://example.com/webhook",
                    params={"Body": "Hello"},
                    signature="valid_signature",
                )

                assert result is True

    def test_validate_request_invalid_signature(self, sms_service: SMSService) -> None:
        """Test that validation fails with invalid signature."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_token"

            with patch.object(sms_service, "_validator") as mock_validator:
                mock_validator.validate.return_value = False

                result = sms_service.validate_request(
                    url="http://example.com/webhook",
                    params={"Body": "Hello"},
                    signature="invalid_signature",
                )

                assert result is False


class TestSMSServiceSendSMS:
    """Tests for the send_sms method."""

    @pytest.mark.asyncio
    async def test_send_sms_no_credentials(self, sms_service: SMSService) -> None:
        """Test that send_sms returns mock_sid when no credentials are configured."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""

            result = await sms_service.send_sms("+15551234567", "Hello!")

            assert result == "mock_sid"

    @pytest.mark.asyncio
    async def test_send_sms_success(self, sms_service: SMSService) -> None:
        """Test successful SMS sending."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"
            mock_settings.twilio_phone_number = "+15559999999"

            mock_client = MagicMock()
            mock_message = MagicMock()
            mock_message.sid = "SM123456"
            mock_client.messages.create.return_value = mock_message

            with patch.object(sms_service, "_client", mock_client):
                result = await sms_service.send_sms("+15551234567", "Hello!")

            assert result == "SM123456"
            mock_client.messages.create.assert_called_once_with(
                body="Hello!",
                from_="+15559999999",
                to="+15551234567",
            )

    @pytest.mark.asyncio
    async def test_send_sms_error(self, sms_service: SMSService) -> None:
        """Test SMS sending error handling."""
        with patch("app.services.sms_service.settings") as mock_settings:
            mock_settings.twilio_account_sid = "test_sid"
            mock_settings.twilio_auth_token = "test_token"
            mock_settings.twilio_phone_number = "+15559999999"

            mock_client = MagicMock()
            mock_client.messages.create.side_effect = Exception("Twilio error")

            with patch.object(sms_service, "_client", mock_client):
                result = await sms_service.send_sms("+15551234567", "Hello!")

            assert result is None


class TestSMSServiceBookingNotifications:
    """Tests for booking notification methods."""

    @pytest.mark.asyncio
    async def test_send_booking_confirmation(self, sms_service: SMSService) -> None:
        """Test sending booking confirmation."""
        with patch.object(sms_service, "send_sms") as mock_send:
            mock_send.return_value = "mock_sid"

            result = await sms_service.send_booking_confirmation(
                "+15551234567",
                "Saturday, December 20 at 08:00 AM for 4 players",
            )

            assert result == "mock_sid"
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0]
            assert call_args[0] == "+15551234567"
            assert "confirmed" in call_args[1].lower()
            assert "Saturday, December 20" in call_args[1]

    @pytest.mark.asyncio
    async def test_send_booking_failure(self, sms_service: SMSService) -> None:
        """Test sending booking failure notification."""
        with patch.object(sms_service, "send_sms") as mock_send:
            mock_send.return_value = "mock_sid"

            result = await sms_service.send_booking_failure(
                "+15551234567",
                "Time slot not available",
            )

            assert result == "mock_sid"
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0]
            assert call_args[0] == "+15551234567"
            assert "unable to book" in call_args[1].lower()
            assert "Time slot not available" in call_args[1]

    @pytest.mark.asyncio
    async def test_send_booking_failure_with_alternatives(self, sms_service: SMSService) -> None:
        """Test sending booking failure with alternatives."""
        with patch.object(sms_service, "send_sms") as mock_send:
            mock_send.return_value = "mock_sid"

            result = await sms_service.send_booking_failure(
                "+15551234567",
                "Time slot not available",
                alternatives="8:08 AM, 8:16 AM",
            )

            assert result == "mock_sid"
            call_args = mock_send.call_args[0]
            assert "alternatives" in call_args[1].lower()
            assert "8:08 AM, 8:16 AM" in call_args[1]

    @pytest.mark.asyncio
    async def test_send_weekly_prompt(self, sms_service: SMSService) -> None:
        """Test sending weekly prompt."""
        with patch.object(sms_service, "send_sms") as mock_send:
            mock_send.return_value = "mock_sid"

            result = await sms_service.send_weekly_prompt("+15551234567")

            assert result == "mock_sid"
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0]
            assert call_args[0] == "+15551234567"
            assert "tee times" in call_args[1].lower()


class TestSMSServiceProperties:
    """Tests for SMSService properties."""

    def test_client_property_cached(self, sms_service: SMSService) -> None:
        """Test that client property is cached."""
        with patch("app.services.sms_service.Client") as mock_client_class:
            mock_client_class.return_value = MagicMock()

            client1 = sms_service.client
            client2 = sms_service.client

            assert client1 is client2
            mock_client_class.assert_called_once()

    def test_validator_property_cached(self, sms_service: SMSService) -> None:
        """Test that validator property is cached."""
        with patch("app.services.sms_service.RequestValidator") as mock_validator_class:
            mock_validator_class.return_value = MagicMock()

            validator1 = sms_service.validator
            validator2 = sms_service.validator

            assert validator1 is validator2
            mock_validator_class.assert_called_once()
