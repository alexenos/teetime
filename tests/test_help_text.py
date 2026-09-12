"""Tests for the usage guidance sent to a user who asked for nothing."""

from app.services.help_text import (
    COMMAND_EXAMPLES,
    RESERVATION_WINDOW,
    addressing_help_message,
    command_examples,
    help_message,
)


class TestCommandExamples:
    def test_every_example_listed(self) -> None:
        block = command_examples()
        for example in COMMAND_EXAMPLES:
            assert f"- {example}" in block

    def test_mention_prefixes_every_example(self) -> None:
        """In a group the addressing is the part the user has to get right, so
        it belongs in the examples rather than only in prose."""
        block = command_examples("@teetimebot ")
        for example in COMMAND_EXAMPLES:
            assert f"- @teetimebot {example}" in block


class TestHelpMessage:
    def test_names_the_club_and_the_window(self) -> None:
        text = help_message()
        assert "Northgate" in text
        assert RESERVATION_WINDOW in text

    def test_carries_examples_without_addressing(self) -> None:
        assert "- Book Saturday 8am for 4 players" in help_message()


class TestAddressingHelpMessage:
    def test_group_variant_says_to_use_one_message(self) -> None:
        text = addressing_help_message("@teetimebot ")
        assert "same message" in text
        assert "@teetimebot Book Saturday 8am for 4 players" in text
        assert RESERVATION_WINDOW in text

    def test_group_variant_quotes_the_handle_without_its_trailing_space(self) -> None:
        assert "@teetimebot on its own" in addressing_help_message("@teetimebot ")

    def test_private_variant_omits_addressing_instructions(self) -> None:
        """No handle to prepend (a private chat, or getMe never resolved) means
        there is no addressing to explain - and none is invented."""
        text = addressing_help_message()
        assert "@" not in text
        assert "- Book Saturday 8am for 4 players" in text
        assert RESERVATION_WINDOW in text
