"""Usage guidance sent back to a user who has not given the bot anything to do.

Two shapes of the same content:

- :func:`help_message` answers an explicit "help" intent, the way it always
  has, and is what ``BookingService`` hands back for ``intent="help"``.
- :func:`addressing_help_message` answers a message that addressed the bot and
  stopped there - "@NorthgateTeetimebot" alone in a group, with the actual
  request sent as a separate, untagged message. The bot never receives that
  second message (with Telegram group privacy mode on it is not delivered at
  all, and unaddressed group text is dropped in ``app/api/webhooks.py``), so
  the request looks ignored from the user's side. Saying what to do
  differently is the whole point of this variant.

Kept in its own module rather than on ``BookingService`` so the inbound edges
that never construct a booking - the Telegram webhook route and the Discord
gateway - can answer a bare mention without importing the service singleton.
"""

# Examples of things the bot understands, shown wherever it explains itself.
# Written as whole messages so a user can copy one and send it as is.
#
# Booking only, deliberately. "Check my bookings" and "Cancel my booking" still
# work as intents - nothing here disables them - but they are not reliable
# enough yet to put in front of someone who has just been told what to type.
# An example is a promise, and the two that are not ready do not belong in one.
# Put them back once the status and cancellation flows are trusted.
COMMAND_EXAMPLES = (
    "Book Saturday 8am for 4 players",
    "Book 9/20 at 12p for 2",
)

# Repeated at the end of every help reply: the single fact that explains why a
# request for tomorrow goes nowhere.
RESERVATION_WINDOW = "Reservations open 7 days in advance at 6:30am CT."


def command_examples(mention: str = "") -> str:
    """The example commands as a bulleted block.

    Args:
        mention: Addressing to prepend to each example, including its trailing
            space ("@NorthgateTeetimebot "). Empty for a private chat, where a
            plain message already reaches the bot. In a group the addressing is
            part of what the user has to get right, so it belongs in the
            examples rather than being described above them.
    """
    return "\n".join(f"- {mention}{example}" for example in COMMAND_EXAMPLES)


def help_message() -> str:
    """The general "what can you do" reply."""
    return (
        "I can help you book tee times at Northgate Country Club!\n\n"
        "Try saying:\n"
        f"{command_examples()}\n\n"
        f"{RESERVATION_WINDOW}"
    )


def addressing_help_message(mention: str = "") -> str:
    """The reply for a message that addressed the bot but asked for nothing.

    Args:
        mention: How this user has to address the bot in this conversation,
            with its trailing space. Empty in a private chat (and whenever the
            bot's own handle could not be resolved), which drops the
            same-message instruction: there is no addressing to get wrong.
    """
    if mention:
        opening = (
            f"I'm here, but I only read the message that tags me - so put the request in "
            f"that same message. {mention.strip()} on its own gives me nothing to book, and "
            "a follow-up sent without tagging me never reaches me."
        )
    else:
        opening = (
            "I'm here, but that message had nothing to act on. Tell me what you'd like in "
            "the message itself."
        )
    return f"{opening}\n\nTry:\n{command_examples(mention)}\n\n{RESERVATION_WINDOW}"
