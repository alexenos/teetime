"""
Booking service for managing tee time reservations.

This module provides the core business logic for handling SMS conversations,
processing booking requests, and executing reservations at the scheduled time.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.config import settings
from app.models.schemas import (
    BookingStatus,
    ConversationState,
    ParsedIntent,
    TeeTimeBooking,
    TeeTimeRequest,
    UserSession,
)
from app.providers.base import BatchBookingRequest, BookingResult, ReservationProvider
from app.services.credential_service import credential_service
from app.services.database_service import database_service
from app.services.gemini_service import gemini_service
from app.services.help_text import help_message
from app.services.proxy_booking import is_proxy_admin, split_proxy_target, strip_leading_for
from app.services.sms_service import sms_service
from app.utils.timezone import CTDateTime

logger = logging.getLogger(__name__)

# Error recorded on bookings that were still IN_PROGRESS when the process died.
# The club website may or may not have accepted the reservation, so the message
# deliberately tells the user to verify rather than asserting either outcome.
INTERRUPTED_ERROR_MESSAGE = (
    "The booking attempt was interrupted before it finished (the service "
    "restarted mid-attempt). The reservation may or may not have gone through - "
    "please check the club website before rebooking."
)

# Replies that settle a pending booking confirmation without asking the LLM
# what they mean. The bot just printed the bookings and said "Reply 'yes' to
# confirm", so a bare "yes" needs no interpretation - and sending it to Gemini
# anyway means a transient API failure can lose an already-parsed booking at
# the one moment it costs the most.
#
# Matching is exact, on the normalized message, never substring: "yes" is
# inside "yesterday" and "no" is inside "no, make it 5:30", and both of those
# are real replies that still need parsing. Anything not listed here falls
# through to the LLM unchanged, so this set is deliberately short - it holds
# only replies with exactly one meaning in this state.
AFFIRMATIVE_CONFIRMATIONS = frozenset(
    {
        "yes",
        "y",
        "ya",
        "yah",
        "yeah",
        "yep",
        "yup",
        "yes please",
        "ok",
        "okay",
        "k",
        "sure",
        "confirm",
        "confirmed",
        "do it",
        "book it",
        "go ahead",
        "send it",
        "sounds good",
        "perfect",
        "👍",
    }
)

# "cancel" and "stop" are deliberately absent: they still go to the LLM,
# because they may mean "cancel my existing bookings" rather than "don't book
# this one", and that distinction is not ours to guess here.
NEGATIVE_CONFIRMATIONS = frozenset(
    {
        "no",
        "n",
        "nope",
        "nah",
        "no thanks",
        "no thank you",
    }
)


def _normalize_reply(message: str) -> str:
    """Reduce a message to a comparable form: lowercase, no edge punctuation.

    Collapses internal whitespace so "yes  please" matches, and strips
    surrounding punctuation so "Yes!" and "yes." do too. Punctuation *inside*
    the message is left alone, so "yes, make it 5:30" stays unmatched and goes
    to the LLM where it belongs.
    """
    return " ".join(message.strip().lower().strip(".,!?;:'\" ").split())


# Replies that abandon a "for which user?" prompt instead of answering it.
# Without these the admin would be stuck re-reading the question: every other
# reply in that state is treated as a name to look up, so "never mind" would
# come back as "I don't know who never is".
PROXY_TARGET_ABORTS = frozenset(
    {"cancel", "nevermind", "never mind", "stop", "forget it", "no", "nvm", "abort"}
)


@dataclass(frozen=True)
class _BookingAttribution:
    """Whose booking this becomes, and where its result is sent.

    For an ordinary requester every field is simply their own session's. For a
    proxy admin booking on a friend's behalf (issue #185) they are the
    friend's: the record carries the friend's identity, so it shows up in the
    friend's history and books under the friend's Walden membership, and the
    confirmation or failure days later goes to the friend's own conversation -
    exactly as if they had asked for it themselves. The admin sees only the
    immediate acknowledgement, in their own chat.
    """

    phone_number: str
    origin_channel_id: str | None
    channel: str | None
    requester_handle: str | None
    # What to call the friend when telling the admin what was booked. None when
    # this is the requester's own booking and there is nobody else to name.
    display_name: str | None

    @property
    def is_proxy(self) -> bool:
        return self.display_name is not None

    def suffix(self) -> str:
        """ " for Alex", or "" for an ordinary booking - to append to a reply."""
        return f" for {self.display_name}" if self.display_name else ""


class BookingService:
    """
    Manages tee time booking requests and SMS conversations.

    This service handles the full lifecycle of booking requests:
    1. Receiving and parsing SMS messages via Gemini LLM
    2. Managing conversation state to collect booking details
    3. Scheduling booking jobs for execution at the reservation open time
    4. Executing bookings via the reservation provider
    5. Sending confirmation/failure notifications via SMS

    Uses database storage for sessions and bookings via DatabaseService,
    ensuring data persists across restarts.

    Attributes:
        _reservation_provider: Provider for executing bookings on the club website.
    """

    def __init__(self) -> None:
        """Initialize the booking service."""
        self._reservation_provider: ReservationProvider | None = None
        # Set instead of _reservation_provider when a provider is built per
        # requester from their own credentials - see _provider_for and the two
        # setters below.
        self._reservation_provider_factory: Callable[[str, str], ReservationProvider] | None = None
        # Strong references to in-flight background booking tasks. asyncio only
        # holds weak references to tasks, so without this a booking run could be
        # garbage collected mid-attempt.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # When this instance came up, in the same naive-UTC space the database
        # stamps onto updated_at. Startup reconciliation uses it to tell rows
        # orphaned by a previous process from attempts running right now.
        self._started_at = datetime.now(UTC).replace(tzinfo=None)

    def set_reservation_provider(self, provider: ReservationProvider) -> None:
        """Use this one provider instance for every requester.

        No longer what production installs - see
        set_reservation_provider_factory, which builds one per requester from
        their own login. This mode remains for the cases where a single
        instance is the honest answer: MockWaldenProvider in local development,
        where there are no credentials at all, and tests that assert against a
        provider they configured.

        It does NOT bypass the credential requirement. _provider_for still
        refuses a requester with no login on file before it gets here, in this
        mode exactly as in the other, so nothing reaches the club under an
        account that is not theirs.
        """
        self._reservation_provider = provider
        self._reservation_provider_factory = None

    def set_reservation_provider_factory(
        self, factory: Callable[[str, str], ReservationProvider]
    ) -> None:
        """Build a fresh provider per requester from their own Walden login.

        This is what production installs (see app/main.py). The factory is the
        provider class itself, called with (member_number, password), so each
        booking drives a session logged in as the person it is for - a provider
        per run, which is #144's option 3.
        """
        self._reservation_provider_factory = factory
        self._reservation_provider = None

    async def _provider_for(self, phone_number: str) -> ReservationProvider | None:
        """The provider this requester's booking should run under.

        Every requester books under their own admin-added Walden login (issue
        #179), as a provider built for that one run - #144's option 3. None is
        returned only when no provider is configured at all, which is the
        "system not set up" case every caller already reports.

        Raises WaldenCredentialRequiredError when the requester has no login on
        file. There is deliberately nothing to fall back to: the shared account
        this used to reach for meant an unconfigured friend silently booked
        under someone else's membership, and the club's
        one-round-per-member-per-day rule made that actively harmful rather than
        merely untidy. ProxyAdminHasNoCredentialError is the same refusal for
        the proxy admin (issue #185), which has no login by design.

        Every caller already treats an exception from this lookup as a loud
        booking failure, so refusing here cannot be mistaken for a success.

        Making these per-requester runs execute concurrently with each other is
        a follow-up (issue #179 flags real open questions there: Cloud Run
        resource limits and untested club-side behavior under simultaneous
        sessions); callers here still run one requester group at a time.
        """
        if self._reservation_provider is None and self._reservation_provider_factory is None:
            return None

        # Checked before either mode builds anything, so a single installed
        # instance is no more of a way around the requirement than a factory is.
        dedicated = await credential_service.require_credentials(phone_number)

        if self._reservation_provider_factory is not None:
            return self._reservation_provider_factory(dedicated.member_number, dedicated.password)

        # One fixed instance (local Mock, or a test's own stub): hand it back as
        # it was installed rather than reconstructing its class, which for a
        # mock would produce a different object than the caller is asserting on.
        assert self._reservation_provider is not None
        return self._reservation_provider

    async def close_reservation_provider(self) -> None:
        """Close the one fixed provider instance, if that is what is installed.

        In factory mode there is nothing to close here: each requester's run
        builds its own provider and _release_provider closes it when that run
        ends, so no provider outlives the booking it was made for.
        """
        if self._reservation_provider is not None:
            await self._reservation_provider.close()

    async def _release_provider(self, provider: ReservationProvider) -> None:
        """Close a provider built for one requester's run.

        Leaves a fixed installed instance alone - that one is owned by whoever
        installed it (local Mock, or a test) and closed at shutdown instead. In
        factory mode every provider is per-run, so every provider is closed.
        """
        if provider is not self._reservation_provider:
            await provider.close()

    async def get_session(self, phone_number: str) -> UserSession:
        """Get or create a session for the given phone number."""
        return await database_service.get_or_create_session(phone_number)

    async def update_session(self, session: UserSession) -> None:
        """Update the session's last interaction time and save it."""
        session.last_interaction = datetime.now(UTC).replace(tzinfo=None)
        await database_service.update_session(session)

    async def handle_incoming_message(
        self,
        phone_number: str,
        message: str,
        origin_channel_id: str | None = None,
        channel: str | None = None,
        requester_handle: str | None = None,
    ) -> str:
        """
        Process an incoming SMS message and return a response.

        This is the main entry point for SMS messages. It:
        1. Gets or creates a session for the user
        2. Builds context from the current conversation state
        3. Parses the message using Gemini LLM
        4. Processes the parsed intent and generates a response
        5. Updates the session state

        Args:
            phone_number: The sender's phone number.
            message: The text content of the SMS.
            origin_channel_id: For Discord and Telegram, the channel or chat
                this message arrived in. Stored on the session and copied onto
                any booking created from this conversation, so the booking
                result days later replies in the same place. None for SMS,
                which has no channels.
            channel: Which messaging channel the message arrived over
                ("discord", "telegram" or "twilio"). Stored alongside
                origin_channel_id and copied onto bookings for the same reason:
                Discord and Telegram IDs are both bare numbers, so the channel
                is the only thing that says which one to answer on. None leaves
                the session's existing channel untouched.
            requester_handle: Ready-to-prepend mention of the sender in a
                shared group (see telegram_provider.addressee_prefix). Stored
                on the session and copied onto any booking created from this
                conversation, so its result notification says who it was for.
                Not passed at all (None) leaves the session's existing value
                untouched; an explicit "" clears it, for a caller like a
                private chat that has no addressing concept.

        Returns:
            The response message to send back to the user.
        """
        session = await self.get_session(phone_number)
        if origin_channel_id:
            session.origin_channel_id = origin_channel_id
        if channel:
            session.channel = channel
        if requester_handle is not None:
            session.requester_handle = requester_handle or None

        # Resolve who this message is on behalf of before anything reads it.
        # Only the single configured admin ID can be booking for someone else;
        # for everyone else this is a no-op and the message is untouched.
        if is_proxy_admin(phone_number):
            early_response, message = await self._prepare_proxy_turn(session, message)
            if early_response is not None:
                await self.update_session(session)
                return early_response

        affirmative = self._confirmation_shortcut(session, message)
        if affirmative is not None:
            response = await self._handle_confirmation_shortcut(session, affirmative)
            await self.update_session(session)
            return response

        context = None
        if session.state != ConversationState.IDLE:
            context = f"Current state: {session.state.value}"
            if session.pending_request:
                context += f", Pending request: {session.pending_request.model_dump_json()}"

        parsed = await gemini_service.parse_message(message, context)

        response = await self._process_intent(session, parsed)

        await self.update_session(session)

        return response

    async def _prepare_proxy_turn(
        self, session: UserSession, message: str
    ) -> tuple[str | None, str]:
        """Settle who the admin is booking for, before the message is parsed.

        Returns ``(early_response, message_to_parse)``. A non-None
        early_response means this turn is already answered - the target could
        not be resolved, or the admin only named one - and nothing else should
        run. Otherwise the message comes back with any "for @X" clause peeled
        off, and session.pending_proxy_target names the friend.

        Called only for the configured admin ID (issue #185).
        """
        if session.state == ConversationState.AWAITING_PROXY_TARGET:
            # We asked "for which user?" last turn, so the whole message is the
            # answer - not a new request.
            if _normalize_reply(message) in PROXY_TARGET_ABORTS:
                session.pending_request = None
                session.pending_requests = None
                session.pending_proxy_target = None
                session.state = ConversationState.IDLE
                return (
                    "Okay, I won't book that. Tell me who it's for when you're ready.",
                    message,
                )

            unresolved = await self._resolve_proxy_target(session, message)
            if unresolved is not None:
                return unresolved, message

            # Target settled; pick the stashed request back up where it left
            # off, so the admin sees the same confirmation they would have got
            # had they named the friend in the first message.
            stashed = ParsedIntent(
                intent="book",
                raw_message=message,
                tee_time_request=session.pending_request,
                tee_time_requests=session.pending_requests,
            )
            session.state = ConversationState.IDLE
            return await self._handle_book_intent(session, stashed), message

        target, remainder = split_proxy_target(message)
        if target is None:
            return None, message

        unresolved = await self._resolve_proxy_target(session, target)
        if unresolved is not None:
            # Drop anything held from an earlier turn. This message named a new
            # target, so its own request was never parsed - and the next reply
            # is read as a name, which would otherwise resume a *previous*
            # booking under it. That is how "for @alex book 9/12" (unconfirmed),
            # then a mistyped "for @nobdy book 9/20", then "@sam" ended up
            # offering Sam the 9/12 slot nobody had asked him about.
            session.pending_request = None
            session.pending_requests = None
            session.pending_proxy_target = None
            return unresolved, message

        if not remainder:
            # "for @alex" and nothing else. The friend is settled; ask for the
            # booking rather than sending an empty message to the parser.
            display_name = await self._proxy_display_name(session)
            return f"Booking for {display_name}. What date and time?", remainder

        return None, remainder

    async def _resolve_proxy_target(self, session: UserSession, target: str) -> str | None:
        """Look "@X" up in the credential store and remember who it is.

        Returns None on success, having set session.pending_proxy_target.
        Returns the message to send back when the target cannot be pinned to
        exactly one friend.

        Both failure modes are deliberately loud, and both leave the session in
        AWAITING_PROXY_TARGET so the next message is read as another attempt at
        the name. Falling back to the shared global account would book somebody
        a round under a membership that isn't theirs - and, with the club's
        one-round-per-member-per-day rule, could quietly consume the slot the
        real booking needed.
        """
        matches = await credential_service.find_by_name_or_telegram_username(target)

        # Verbatim first, the stripped form only as a fallback. "For Ronald" is
        # the natural answer to "reply with their name" and the preposition is
        # not part of the name - but a friend stored as "For Real" has to keep
        # working too, and the order is what makes both true.
        #
        # The reverse order is unsafe rather than merely different: with "For
        # Real" and "Real" both on file, stripping first resolves a reply of
        # "For Real" to "Real" and books a round under the wrong membership -
        # the one outcome this whole path exists to make impossible. Trying the
        # literal first means an exact stored name always wins, and the strip
        # only ever runs when nothing matched it, where there is no competing
        # reading left to get wrong.
        if not matches:
            stripped = strip_leading_for(target)
            if stripped != target:
                matches = await credential_service.find_by_name_or_telegram_username(stripped)

        if not matches:
            session.state = ConversationState.AWAITING_PROXY_TARGET
            logger.info("Proxy target %r matched no stored credential", target)
            return (
                f'I don\'t know who "{target}" is - nobody with that name or Telegram '
                "handle has a stored Walden login. Add one with "
                "add_walden_credential.py set <id> --name ... --telegram-username ..., "
                "or tell me a different name."
            )

        if len(matches) > 1:
            session.state = ConversationState.AWAITING_PROXY_TARGET
            names = ", ".join(sorted(owner.display_name for owner in matches))
            logger.warning("Proxy target %r matched %d credentials", target, len(matches))
            return (
                f'"{target}" matches more than one person ({names}). '
                "Tell me which one, using a name or handle that only fits them."
            )

        owner = matches[0]
        session.pending_proxy_target = owner.phone_number
        logger.info("Proxy booking target %r resolved to %s", target, owner.phone_number)
        return None

    async def _proxy_display_name(self, session: UserSession) -> str:
        """What to call the friend this session is currently booking for."""
        attribution = await self._attribution_for(session)
        return attribution.display_name or attribution.phone_number

    async def _attribution_for(self, session: UserSession) -> _BookingAttribution:
        """Whose booking this session's pending request becomes.

        The admin's own conversation stays theirs - the echo-back and the
        "reply yes" land in their chat, where they typed. Only the booking
        record is the friend's, and with it the notification days later, which
        goes to wherever that friend last talked to the bot (their group, or
        their private chat) rather than to the admin.
        """
        target = session.pending_proxy_target
        if not target or not is_proxy_admin(session.phone_number):
            return _BookingAttribution(
                phone_number=session.phone_number,
                origin_channel_id=session.origin_channel_id,
                channel=session.channel,
                requester_handle=session.requester_handle,
                display_name=None,
            )

        owner = await credential_service.get_owner(target)
        friend_session = await database_service.get_session(target)

        requester_handle = None
        if friend_session is not None and friend_session.requester_handle:
            requester_handle = friend_session.requester_handle
        elif owner is not None and owner.telegram_username:
            # They have never spoken to the bot in a group, so there is no
            # captured mention - build one from the stored handle so a result
            # landing in a shared chat still names the right person.
            requester_handle = f"@{owner.telegram_username} "

        return _BookingAttribution(
            phone_number=target,
            origin_channel_id=friend_session.origin_channel_id if friend_session else None,
            # The friend's own channel when we know it; otherwise the admin's,
            # which is the channel their identity was just resolved on.
            channel=(friend_session.channel if friend_session else None) or session.channel,
            requester_handle=requester_handle,
            display_name=owner.display_name if owner else target,
        )

    def _clear_pending_proxy_target(self, session: UserSession) -> None:
        """Forget who the admin was booking for, now that the flow has ended.

        Held only for the length of one booking conversation. Left set, the
        admin's next unrelated "book 9/20 at 8a" would silently go to whoever
        they happened to name last - precisely the kind of misattributed
        booking this feature must not produce.
        """
        session.pending_proxy_target = None

    async def _process_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        """Route the parsed intent to the appropriate handler."""
        # Booking for someone else is the whole of v1 (issue #185). Cancelling
        # or listing another friend's bookings is a deliberate follow-up, so
        # "for @alex cancel" is refused rather than quietly applied to the
        # admin's own (empty) history.
        if session.pending_proxy_target and parsed.intent in ("cancel", "modify", "status"):
            display_name = await self._proxy_display_name(session)
            self._clear_pending_proxy_target(session)
            session.state = ConversationState.IDLE
            return (
                f"I can only book on someone else's behalf right now, not {parsed.intent} "
                f"for them. {display_name} can ask me to do that themselves."
            )
        # Check if user is in the middle of selecting a booking to cancel
        # This takes priority over Gemini's parsed intent to avoid misinterpreting
        # date/time responses as new booking requests
        if session.state == ConversationState.AWAITING_CANCELLATION_SELECTION:
            return await self._handle_cancellation_selection(session, parsed)

        if parsed.intent == "book":
            return await self._handle_book_intent(session, parsed)
        elif parsed.intent == "confirm":
            return await self._handle_confirm_intent(session)
        elif parsed.intent == "status":
            return await self._handle_status_intent(session)
        elif parsed.intent == "cancel":
            return await self._handle_cancel_intent(session, parsed)
        elif parsed.intent == "help":
            # Always the curated text, never the parser's own prose. The system
            # prompt describes the bot as able to book, check, cancel and
            # modify, so a model-written help reply advertises all four - while
            # the curated one deliberately offers only what is trusted to work
            # (see app/services/help_text.py). Letting response_message win
            # made the examples someone is shown depend on what the LLM said
            # that turn, which is the one thing help text must not do.
            return self._get_help_message()
        else:
            return (
                parsed.response_message
                or "I'm not sure I understood. Try 'Book Saturday 8am for 4 players'."
            )

    async def _handle_book_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        if parsed.tee_time_requests and len(parsed.tee_time_requests) > 1:
            session.pending_requests = parsed.tee_time_requests
            session.pending_request = None

            ask_target = self._ask_for_proxy_target(session)
            if ask_target is not None:
                return ask_target

            session.state = ConversationState.AWAITING_CONFIRMATION

            booking_summaries = []
            for i, request in enumerate(parsed.tee_time_requests, 1):
                date_str = request.requested_date.strftime("%A, %B %d")
                time_str = request.requested_time.strftime("%I:%M %p")
                booking_summaries.append(
                    f"{i}. {date_str} at {time_str} for {request.num_players} players"
                )

            attribution = await self._attribution_for(session)
            return (
                f"I'll book {len(parsed.tee_time_requests)} tee times{attribution.suffix()}:\n"
                + "\n".join(booking_summaries)
                + "\n\nReply 'yes' to confirm all bookings."
            )

        if not parsed.tee_time_request:
            if parsed.clarification_needed:
                return parsed.clarification_needed
            return "I need more details. What date and time would you like?"

        session.pending_request = parsed.tee_time_request
        session.pending_requests = None

        ask_target = self._ask_for_proxy_target(session)
        if ask_target is not None:
            return ask_target

        session.state = ConversationState.AWAITING_CONFIRMATION

        request = parsed.tee_time_request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")

        attribution = await self._attribution_for(session)
        return (
            f"I'll book a tee time{attribution.suffix()} for {date_str} at {time_str} "
            f"for {request.num_players} players. Reply 'yes' to confirm."
        )

    def _ask_for_proxy_target(self, session: UserSession) -> str | None:
        """Ask the admin who a booking is for, when they didn't say.

        Returns the question (and parks the session in AWAITING_PROXY_TARGET,
        holding the request just parsed) when this is the admin booking with no
        target named, and None in every other case - which is every non-admin
        user, and the admin after a "for @X" clause has already been resolved.

        Asked at this point rather than before parsing because the admin may
        have typed neither a target nor a date ("book me a tee time"). Waiting
        until there is a request to hold means the two prompts cannot fight
        over the same turn: details first, then who it's for, then confirm.
        """
        if not is_proxy_admin(session.phone_number) or session.pending_proxy_target:
            return None

        session.state = ConversationState.AWAITING_PROXY_TARGET
        return (
            "For which user? This account has no Walden login of its own, so every "
            "booking has to be made under a friend's. Reply with their name or "
            "Telegram handle."
        )

    def _confirmation_shortcut(self, session: UserSession, message: str) -> bool | None:
        """Decide whether this message settles a pending booking without the LLM.

        Returns True for an unambiguous yes, False for an unambiguous no, and
        None when the message needs parsing - which is every case except a bare
        confirmation of a booking we have already parsed and echoed back.

        A pending cancellation is excluded on purpose: "yes" there answers "are
        you sure you want to cancel", and that path keeps its existing handling.
        """
        if session.state != ConversationState.AWAITING_CONFIRMATION:
            return None
        if session.pending_cancellation_id:
            return None
        if not session.pending_request and not session.pending_requests:
            return None

        normalized = _normalize_reply(message)
        if normalized in AFFIRMATIVE_CONFIRMATIONS:
            return True
        if normalized in NEGATIVE_CONFIRMATIONS:
            return False
        return None

    async def _handle_confirmation_shortcut(self, session: UserSession, affirmative: bool) -> str:
        """Act on a yes/no that was understood without calling the LLM."""
        logger.info(
            "Settled pending confirmation for %s without an LLM call (affirmative=%s)",
            session.phone_number,
            affirmative,
        )

        if affirmative:
            return await self._handle_confirm_intent(session)

        session.pending_request = None
        session.pending_requests = None
        self._clear_pending_proxy_target(session)
        session.state = ConversationState.IDLE
        return "Okay, I won't book that. Let me know if you'd like a different date or time."

    async def _handle_confirm_intent(self, session: UserSession) -> str:
        if session.state != ConversationState.AWAITING_CONFIRMATION:
            return "There's nothing to confirm. Would you like to book a tee time?"

        if session.pending_requests and len(session.pending_requests) > 0:
            return await self._handle_confirm_multiple_bookings(session)

        if not session.pending_request:
            return "There's nothing to confirm. Would you like to book a tee time?"

        attribution = await self._attribution_for(session)

        try:
            booking = await self.create_booking(
                attribution.phone_number,
                session.pending_request,
                attribution.origin_channel_id,
                channel=attribution.channel,
                requester_handle=attribution.requester_handle,
            )
        except ValueError as e:
            session.pending_request = None
            self._clear_pending_proxy_target(session)
            session.state = ConversationState.IDLE
            return str(e)

        session.pending_request = None
        self._clear_pending_proxy_target(session)
        session.state = ConversationState.IDLE

        request = booking.request
        date_str = request.requested_date.strftime("%A, %B %d")
        time_str = request.requested_time.strftime("%I:%M %p")
        # Who the booking is for, when that is not the person being replied to.
        # The result itself goes to them, not here - so the admin is told that
        # explicitly rather than being left waiting for an outcome message.
        suffix = attribution.suffix()
        result_note = (
            f" {attribution.display_name} will get the result." if attribution.is_proxy else ""
        )

        if booking.status == BookingStatus.SUCCESS:
            booked_time_str = (
                booking.actual_booked_time.strftime("%I:%M %p")
                if booking.actual_booked_time
                else time_str
            )
            return (
                f"Booking confirmed! Reserved {date_str} at {booked_time_str} "
                f"for {request.num_players} players{suffix}."
            )
        elif booking.status == BookingStatus.FAILED:
            return (
                f"Booking attempted{suffix} for {date_str} at {time_str} "
                f"for {request.num_players} players, but it failed. "
                f"I'll text you with more details."
            )
        elif booking.status == BookingStatus.IN_PROGRESS:
            # The booking window is already open, so the attempt is running right
            # now in the background. This is the acknowledgement the user gets
            # up front; the outcome arrives as a separate message.
            return (
                f"On it - booking {date_str} at {time_str} "
                f"for {request.num_players} players{suffix} now. "
                f"This takes a minute or two; I'll message you with the result." + result_note
            )

        exec_time = booking.scheduled_execution_time
        if exec_time:
            exec_str = exec_time.strftime("%A at %I:%M %p CT")
            return (
                f"Booking scheduled{suffix}! I'll attempt to reserve {date_str} at {time_str} "
                f"for {request.num_players} players. The booking window opens {exec_str}. "
                f"I'll text you with the result." + result_note
            )
        else:
            return (
                f"Booking request received{suffix} for {date_str} at {time_str} "
                f"for {request.num_players} players. I'll text you with updates." + result_note
            )

    async def _handle_confirm_multiple_bookings(self, session: UserSession) -> str:
        """Handle confirmation of multiple booking requests."""
        if not session.pending_requests:
            return "There's nothing to confirm. Would you like to book a tee time?"

        successful_bookings: list[TeeTimeBooking] = []
        failed_requests: list[tuple[TeeTimeRequest, str]] = []

        attribution = await self._attribution_for(session)

        for request in session.pending_requests:
            try:
                booking = await self.create_booking(
                    attribution.phone_number,
                    request,
                    attribution.origin_channel_id,
                    defer_execution=True,
                    channel=attribution.channel,
                    requester_handle=attribution.requester_handle,
                )
                successful_bookings.append(booking)
            except ValueError as e:
                failed_requests.append((request, str(e)))

        session.pending_requests = None
        session.pending_request = None
        self._clear_pending_proxy_target(session)
        session.state = ConversationState.IDLE

        # Bookings whose window is already open run as ONE batch: a single
        # driver, a single login, attempts made in sequence. Letting
        # create_booking spawn its own task per booking put two headless Chromes
        # on the club's login form ~80ms apart; the portal accepted one and left
        # the other sitting on the login page, failing that booking outright.
        immediate_booking_ids = [
            booking.id
            for booking in successful_bookings
            if booking.status != BookingStatus.SCHEDULED and booking.id is not None
        ]
        if immediate_booking_ids:
            self._spawn_bookings_batch_execution(immediate_booking_ids)

        response_parts = []

        if successful_bookings:
            scheduled_bookings = [
                b for b in successful_bookings if b.status == BookingStatus.SCHEDULED
            ]
            immediate_bookings = [
                b for b in successful_bookings if b.status != BookingStatus.SCHEDULED
            ]

            if scheduled_bookings:
                booking_summaries = []
                for booking in scheduled_bookings:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    time_str = booking.request.requested_time.strftime("%I:%M %p")
                    booking_summaries.append(
                        f"- {date_str} at {time_str} for {booking.request.num_players} players"
                    )

                if len(scheduled_bookings) == 1:
                    exec_time = scheduled_bookings[0].scheduled_execution_time
                    exec_str = exec_time.strftime("%A at %I:%M %p CT") if exec_time else "soon"
                    response_parts.append(
                        f"Booking scheduled! The booking window opens {exec_str}:\n"
                        + "\n".join(booking_summaries)
                    )
                else:
                    response_parts.append(
                        f"{len(scheduled_bookings)} bookings scheduled! "
                        "I'll text you with results when the booking windows open:\n"
                        + "\n".join(booking_summaries)
                    )

            if immediate_bookings:
                for booking in immediate_bookings:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    time_str = booking.request.requested_time.strftime("%I:%M %p")
                    if booking.status == BookingStatus.SUCCESS:
                        booked_time_str = (
                            booking.actual_booked_time.strftime("%I:%M %p")
                            if booking.actual_booked_time
                            else time_str
                        )
                        response_parts.append(
                            f"Booking confirmed! Reserved {date_str} at {booked_time_str} "
                            f"for {booking.request.num_players} players."
                        )
                    elif booking.status == BookingStatus.FAILED:
                        response_parts.append(
                            f"Booking for {date_str} at {time_str} failed. "
                            "I'll text you with more details."
                        )
                    elif booking.status == BookingStatus.IN_PROGRESS:
                        response_parts.append(
                            f"On it - booking {date_str} at {time_str} now. "
                            "I'll message you with the result."
                        )

        if failed_requests:
            for request, error in failed_requests:
                date_str = request.requested_date.strftime("%A, %B %d")
                time_str = request.requested_time.strftime("%I:%M %p")
                response_parts.append(f"Could not schedule {date_str} at {time_str}: {error}")

        if not response_parts:
            return "No bookings were created."

        if attribution.is_proxy:
            # Said once, up front, rather than threaded through every line
            # above: these are all the same friend's bookings, and the admin
            # needs to know the outcomes go to them rather than here.
            response_parts.insert(0, f"Booking for {attribution.display_name}.")
            response_parts.append(f"{attribution.display_name} will get the results.")

        return "\n\n".join(response_parts)

    async def _handle_status_intent(self, session: UserSession) -> str:
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)

        if not user_bookings:
            return "You don't have any scheduled bookings. Would you like to book a tee time?"

        today = CTDateTime.now().date()
        upcoming = [b for b in user_bookings if b.request.requested_date >= today]

        # IN_PROGRESS and SUCCESS belong here alongside PENDING/SCHEDULED. Leaving
        # them out once made a booking that was mid-attempt look like it had never
        # been created at all, which is worse than showing an unfinished state.
        active = [
            b
            for b in upcoming
            if b.status
            in [
                BookingStatus.PENDING,
                BookingStatus.SCHEDULED,
                BookingStatus.IN_PROGRESS,
                BookingStatus.SUCCESS,
            ]
        ]
        # Failures are only worth surfacing for tee times that haven't happened
        # yet - those are the ones the user can still do something about.
        failed = [b for b in upcoming if b.status == BookingStatus.FAILED]

        if not active and not failed:
            return "You don't have any upcoming bookings. Would you like to book a tee time?"

        def describe(booking: TeeTimeBooking) -> str:
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            if booking.status == BookingStatus.SUCCESS and booking.actual_booked_time:
                time_str = booking.actual_booked_time.strftime("%I:%M %p")
            return f"- {date_str} at {time_str}: {booking.status.value}"

        active.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))
        failed.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))

        sections = []
        if active:
            sections.append("Your upcoming bookings:\n" + "\n".join(describe(b) for b in active))
        if failed:
            failure_lines = []
            for booking in failed:
                line = describe(booking)
                if booking.error_message:
                    line += f"\n  {booking.error_message}"
                failure_lines.append(line)
            sections.append("Recent failures:\n" + "\n".join(failure_lines))

        return "\n\n".join(sections)

    async def reconcile_interrupted_bookings(self) -> list[TeeTimeBooking]:
        """Resolve bookings left IN_PROGRESS by a crash or restart.

        A booking attempt drives Selenium for a minute or more with the row held
        at IN_PROGRESS. If the process dies in that window (an OOM kill, a
        deploy, a Cloud Run instance replacement), nothing ever moves the row
        off IN_PROGRESS and nothing tells the user - the request simply goes
        quiet.

        Only rows last touched before this process started are treated as
        orphans. This instance can set IN_PROGRESS itself - through the REST
        API, the scheduler's batch job, or a confirmed chat booking - and those
        attempts are still running, so failing them here would both lie to the
        user and clobber the real outcome they are about to write.

        Each orphan is marked FAILED with an explanation that the reservation's
        true state is unknown, and the user is notified. Callers should invoke
        this once at startup, after the messaging channel is ready.

        Returns:
            The bookings that were successfully reconciled.
        """
        in_progress = await database_service.get_bookings(status=BookingStatus.IN_PROGRESS)
        orphaned = [b for b in in_progress if b.updated_at < self._started_at]
        if not orphaned:
            return []

        live = len(in_progress) - len(orphaned)
        logger.warning(
            "Found %d booking(s) stuck IN_PROGRESS from a previous run; marking failed "
            "(%d in-progress booking(s) started by this instance left alone)",
            len(orphaned),
            live,
        )

        reconciled: list[TeeTimeBooking] = []
        for booking in orphaned:
            booking.status = BookingStatus.FAILED
            booking.error_message = INTERRUPTED_ERROR_MESSAGE

            try:
                await database_service.update_booking(booking)
            except Exception:
                # One unwritable row must not strand every other orphan. Skip it
                # and leave it for the next startup rather than telling the user
                # about a failure we could not actually record.
                logger.exception("Could not reconcile interrupted booking %s", booking.id)
                continue

            reconciled.append(booking)

            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            booking_details = f"{date_str} at {time_str} for {booking.request.num_players} players"
            logger.warning("Reconciled interrupted booking %s (%s)", booking.id, booking_details)

            try:
                await sms_service.send_booking_failure(
                    booking.phone_number,
                    INTERRUPTED_ERROR_MESSAGE,
                    booking_details=booking_details,
                    origin_channel_id=booking.origin_channel_id,
                    channel=booking.channel,
                    requester_handle=booking.requester_handle,
                )
            except Exception:
                # A notification failure must not stop us reconciling the rest.
                logger.exception("Could not notify user about interrupted booking %s", booking.id)

        return reconciled

    async def _handle_cancel_intent(self, session: UserSession, parsed: ParsedIntent) -> str:
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)
        cancellable = [
            b
            for b in user_bookings
            if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED, BookingStatus.SUCCESS]
        ]

        if not cancellable:
            return "You don't have any bookings to cancel."

        # Check if user is confirming a pending cancellation
        if session.pending_cancellation_id:
            # User is responding to a confirmation prompt
            message_lower = parsed.raw_message.lower() if parsed.raw_message else ""
            if any(word in message_lower for word in ["yes", "confirm", "ok", "sure", "y"]):
                booking = await database_service.get_booking(session.pending_cancellation_id)
                if booking:
                    date_str = booking.request.requested_date.strftime("%A, %B %d")
                    session.pending_cancellation_id = None
                    await self.update_session(session)

                    if booking.status == BookingStatus.SUCCESS:
                        success = await self._cancel_confirmed_booking(booking)
                        if success:
                            return (
                                f"Your confirmed booking for {date_str} has been cancelled "
                                "on the website."
                            )
                        else:
                            return (
                                f"I was unable to cancel your booking for {date_str} on the "
                                "website. Please contact the club directly to cancel."
                            )
                    else:
                        booking.status = BookingStatus.CANCELLED
                        await database_service.update_booking(booking)
                        return f"Your booking for {date_str} has been cancelled."
            else:
                # User declined or gave unclear response
                session.pending_cancellation_id = None
                await self.update_session(session)
                return "Cancellation cancelled. Your booking remains active."

        # Always ask for confirmation before cancelling, even for single bookings
        if len(cancellable) == 1:
            booking = cancellable[0]
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_label = "confirmed" if booking.status == BookingStatus.SUCCESS else "scheduled"

            # Store the pending cancellation and ask for confirmation
            session.pending_cancellation_id = booking.id
            await self.update_session(session)

            return (
                f"Are you sure you want to cancel your {status_label} booking for "
                f"{date_str} at {time_str}? Reply 'yes' to confirm."
            )

        # Multiple bookings - ask which one to cancel and set state
        # Sort by date/time for consistent ordering when user replies with a number
        cancellable.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))
        session.state = ConversationState.AWAITING_CANCELLATION_SELECTION

        status_lines = []
        for i, booking in enumerate(cancellable, 1):
            date_str = booking.request.requested_date.strftime("%A, %B %d")
            time_str = booking.request.requested_time.strftime("%I:%M %p")
            status_label = "confirmed" if booking.status == BookingStatus.SUCCESS else "scheduled"
            players = booking.request.num_players
            status_lines.append(
                f"{i}. {date_str} at {time_str} for {players} players ({status_label})"
            )

        return "Which booking would you like to cancel? Reply with the number.\n" + "\n".join(
            status_lines
        )

    async def _handle_cancellation_selection(
        self, session: UserSession, parsed: ParsedIntent
    ) -> str:
        """
        Handle user's response when selecting which booking to cancel.

        This method is called when the session is in AWAITING_CANCELLATION_SELECTION state,
        meaning we previously asked the user which booking they want to cancel from a list.
        We try to match their response to one of their cancellable bookings using:
        1. A number (e.g., "1", "2", "3") corresponding to the numbered list
        2. A date/time extracted by Gemini as a fallback

        Args:
            session: The user's session.
            parsed: The parsed intent from Gemini (may have extracted date/time info).

        Returns:
            Response message to send to the user.
        """
        user_bookings = await database_service.get_bookings(phone_number=session.phone_number)
        cancellable = [
            b
            for b in user_bookings
            if b.status in [BookingStatus.PENDING, BookingStatus.SCHEDULED, BookingStatus.SUCCESS]
        ]

        if not cancellable:
            session.state = ConversationState.IDLE
            return "You don't have any bookings to cancel."

        # Sort by date/time for consistent ordering (same as when we displayed the list)
        cancellable.sort(key=lambda b: (b.request.requested_date, b.request.requested_time))

        matched_booking = None
        raw_message = (parsed.raw_message or "").strip()

        # First, try to parse as a number (e.g., "1", "2", "3")
        try:
            selection_num = int(raw_message)
            if 1 <= selection_num <= len(cancellable):
                matched_booking = cancellable[selection_num - 1]
        except ValueError:
            pass

        # If not a number, try to match by date/time from Gemini's parsing
        if not matched_booking and parsed.tee_time_request:
            target_date = parsed.tee_time_request.requested_date
            target_time = parsed.tee_time_request.requested_time

            # Find bookings matching the date
            date_matches = [b for b in cancellable if b.request.requested_date == target_date]

            if len(date_matches) == 1:
                matched_booking = date_matches[0]
            elif len(date_matches) > 1:
                # Multiple bookings on same date - try to match by time
                time_matches = [b for b in date_matches if b.request.requested_time == target_time]
                if len(time_matches) == 1:
                    matched_booking = time_matches[0]
                elif len(time_matches) > 1:
                    # Still multiple matches - tell user to use the number
                    return (
                        "You have multiple bookings at that time. "
                        "Please reply with the number (1, 2, 3, etc.) from the list."
                    )
                else:
                    # No exact time match, but multiple date matches
                    # Tell user to use the number
                    return (
                        f"You have multiple bookings on {target_date.strftime('%B %d')}. "
                        "Please reply with the number (1, 2, 3, etc.) from the list."
                    )

        if not matched_booking:
            # Could not match - keep state and ask user to try again with number
            return (
                "I couldn't match that to a booking. "
                "Please reply with the number (1, 2, 3, etc.) from the list."
            )

        # Found a matching booking - set up for confirmation
        date_str = matched_booking.request.requested_date.strftime("%A, %B %d")
        time_str = matched_booking.request.requested_time.strftime("%I:%M %p")
        status_label = (
            "confirmed" if matched_booking.status == BookingStatus.SUCCESS else "scheduled"
        )

        # Store the pending cancellation and transition to confirmation
        session.pending_cancellation_id = matched_booking.id
        session.state = ConversationState.IDLE

        return (
            f"Are you sure you want to cancel your {status_label} booking for "
            f"{date_str} at {time_str}? Reply 'yes' to confirm."
        )

    async def _cancel_confirmed_booking(self, booking: TeeTimeBooking) -> bool:
        """
        Cancel a confirmed booking on the club website.

        This method calls the reservation provider to cancel the booking on the
        actual website, then updates the booking status in the database.

        Args:
            booking: The confirmed booking to cancel.

        Returns:
            True if cancellation was successful, False otherwise.
        """
        booked_time = booking.actual_booked_time or booking.request.requested_time
        cancellation_id = (
            f"{booking.request.requested_date.strftime('%Y-%m-%d')}_{booked_time.strftime('%H:%M')}"
        )

        # _provider_for resolves this requester's credential from the database
        # and can therefore raise (a DB error, or a decryption failure if
        # CREDENTIAL_ENCRYPTION_KEY changed since the row was written) - unlike
        # the synchronous attribute check this replaced. It runs inside the
        # same try/except as the cancellation itself so a lookup failure is
        # reported the same way any other cancellation failure is.
        provider: ReservationProvider | None = None
        try:
            provider = await self._provider_for(booking.phone_number)
            if not provider:
                return False

            success = await provider.cancel_booking(cancellation_id)

            if success:
                booking.status = BookingStatus.CANCELLED
                await database_service.update_booking(booking)
                return True
            else:
                return False

        except Exception as e:
            booking.error_message = f"Cancellation failed: {str(e)}"
            await database_service.update_booking(booking)
            return False
        finally:
            if provider is not None:
                await self._release_provider(provider)

    async def create_booking(
        self,
        phone_number: str,
        request: TeeTimeRequest,
        origin_channel_id: str | None = None,
        defer_execution: bool = False,
        channel: str | None = None,
        requester_handle: str | None = None,
    ) -> TeeTimeBooking:
        """
        Create a new booking record and schedule it for execution.

        This is the public API for creating bookings, used by both the SMS
        conversation flow and the REST API.

        If the calculated execution time is in the past (i.e., the booking window
        has already opened), the attempt starts immediately in the background
        rather than being scheduled for later. This call returns as soon as the
        record is created, with the booking in IN_PROGRESS; the outcome is
        reported to the user by execute_booking when the attempt finishes.

        Multi-player bookings (2+ players) are rejected if the tee time is within
        48 hours, because the Walden Golf website disables TBD guest placeholders
        within that window.

        Args:
            phone_number: The phone number to associate with the booking.
            request: The tee time request details.
            origin_channel_id: Discord channel or Telegram chat this booking was
                requested in, so the success/failure notification replies there.
                None for SMS and REST API callers.
            channel: Messaging channel this booking was requested over, so its
                notification days later goes back over the same one. None for
                REST API callers, which fall back to MESSAGING_CHANNEL.
            requester_handle: Ready-to-prepend mention of who requested this
                booking, so its result notification days later says who it
                was for. None for a private conversation or a channel with no
                addressing concept.
            defer_execution: Create the record and mark it IN_PROGRESS, but do
                not start the attempt. Callers creating several bookings at once
                set this so they can run the whole set as one batch instead of
                one concurrent attempt per booking; they then own both starting
                the work and reporting the outcome.

        Returns:
            The created TeeTimeBooking record.

        Raises:
            ValueError: If multi-player booking is requested within 48 hours, or
                if the booking is attributed to the proxy admin's own identity.
        """
        # The proxy admin books as a friend or not at all (issue #185). Refused
        # here as well as in the credential lookup because this is the point
        # where it is still a conversation: the admin gets told why, instead of
        # a booking record being written that can only fail hours later, at 6:30.
        if is_proxy_admin(phone_number):
            raise ValueError(
                "This admin account has no Walden login of its own, so it can only book "
                "on a friend's behalf. Say who it's for, e.g. \"for @alex book 9/12 at 8a\"."
            )

        # Everyone else needs a login of their own, for the same reason and at
        # the same point. _provider_for refuses this too, but that happens when
        # the attempt runs - which for a scheduled booking is 6:30 a week later,
        # long after the user was told it was booked. Asked and answered here
        # instead, while there is still someone reading the reply.
        if await credential_service.get_dedicated_credentials(phone_number) is None:
            raise ValueError(
                "Your account isn't set up for booking yet - I don't have a Walden "
                "login on file for you, and I won't book under anyone else's. Ask Dax "
                "to add yours, then try again."
            )

        # Check 48-hour restriction for multi-player bookings
        if request.num_players > 1:
            now_ct = CTDateTime.now()

            # Combine requested date and time into a timezone-aware CT datetime
            tee_time_naive = datetime.combine(request.requested_date, request.requested_time)
            tee_time_ct = CTDateTime.from_naive_ct(tee_time_naive)

            hours_until_tee_time = (tee_time_ct - now_ct).total_seconds() / 3600

            if hours_until_tee_time < 48:
                raise ValueError(
                    f"Multi-player bookings ({request.num_players} players) cannot be made "
                    f"within 48 hours of the tee time. The Walden Golf website disables "
                    f"TBD guest placeholders within this window. "
                    f"You can still book for 1 player, or choose a tee time more than 48 hours away."
                )

        booking_id = str(uuid.uuid4())[:8]

        execution_time = self._calculate_execution_time(request.requested_date)

        booking = TeeTimeBooking(
            id=booking_id,
            phone_number=phone_number,
            request=request,
            status=BookingStatus.SCHEDULED,
            scheduled_execution_time=execution_time,
            origin_channel_id=origin_channel_id,
            channel=channel,
            requester_handle=requester_handle,
        )

        created_booking = await database_service.create_booking(booking)

        # Compare in timezone-aware space for robustness.
        # execution_time is naive (CT wall-clock), so we localize it.
        # normalize_to_ct handles both naive and aware inputs safely.
        now_ct = CTDateTime.now()
        exec_ct = CTDateTime.normalize_to_ct(execution_time)

        if exec_ct <= now_ct:
            booking_id_opt: str | None = created_booking.id
            if booking_id_opt is not None:
                # The booking window is already open, so this runs now rather
                # than waiting for the scheduler. Run it as a background task
                # instead of awaiting it: a booking attempt drives Selenium for
                # a minute or more, and the caller (the Discord gateway) cannot
                # send its reply until this returns. Awaiting it here means any
                # crash during the attempt takes the user's reply down with it,
                # leaving them with silence. The caller now acknowledges
                # immediately and the batch reports the real outcome when it
                # lands.
                #
                # As a batch of one, deliberately. The 6:30 race is itself a
                # batch of one, and every race behaviour - fallback ranking, the
                # clock probe, refresh-at-window, the sweep ladder, the precision
                # wait, the race ledger - is gated on `execute_at` being set,
                # which only the batch path supplies. Routed through
                # execute_booking instead, an ad-hoc booking took a path that
                # structurally could not be timed: book_tee_time has no
                # execute_at parameter, so all six switched off and the 08-14
                # ad-hoc booking ran untimed with walden_adhoc_execute_delay_s
                # set to 90. That left the machinery deciding the only booking
                # that matters exercised once a day, unobserved.
                created_booking.status = BookingStatus.IN_PROGRESS
                await database_service.update_booking(created_booking)
                if not defer_execution:
                    self._spawn_bookings_batch_execution([booking_id_opt])

        return created_booking

    def _spawn_booking_execution(self, booking_id: str) -> None:
        """Run execute_booking as a tracked background task."""
        task = asyncio.create_task(
            self._execute_booking_in_background(booking_id),
            name=f"execute-booking-{booking_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _execute_booking_in_background(self, booking_id: str) -> None:
        """Execute a booking, ensuring failures are logged rather than swallowed.

        execute_booking already reports its own outcome to the user and records
        it on the booking row. This wrapper only guards against an exception
        escaping into an unretrieved task result, where it would vanish silently.
        """
        try:
            await self.execute_booking(booking_id)
        except Exception:
            logger.exception("Background booking execution failed for %s", booking_id)

    def _spawn_bookings_batch_execution(self, booking_ids: list[str]) -> None:
        """Run several already-open bookings as one tracked background task."""
        task = asyncio.create_task(
            self._execute_bookings_batch_in_background(booking_ids),
            name=f"execute-bookings-batch-{len(booking_ids)}-from-{booking_ids[0]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _execute_bookings_batch_in_background(self, booking_ids: list[str]) -> None:
        """Attempt a set of bookings in one session, ensuring failures are logged.

        Mirrors _execute_booking_in_background: the work itself reports its own
        outcomes, and this wrapper only stops an exception from escaping into an
        unretrieved task result, where it would vanish silently.
        """
        try:
            await self._run_bookings_batch(booking_ids)
        except Exception:
            logger.exception("Background batch booking execution failed for %s", booking_ids)

    async def _run_bookings_batch(self, booking_ids: list[str]) -> None:
        """Book a set of tee times in one session and report each outcome.

        execute_bookings_batch deliberately leaves notification to its caller,
        so this reports every booking itself. Each of these bookings has already
        been acknowledged to the user, so every path out of here has to end in a
        message per booking - silence would leave them waiting on a reply that
        never comes. That is also why each notification is isolated: one booking
        whose message fails to send must not strand the rest of the batch.
        """
        bookings = [
            booking
            for booking in [await self.get_booking(booking_id) for booking_id in booking_ids]
            if booking is not None
        ]
        if not bookings:
            logger.error("Batch booking execution found no records for %s", booking_ids)
            return

        try:
            results = await self.execute_bookings_batch(
                bookings, execute_at=self._adhoc_execute_at()
            )
            results = await self._retry_blocked_untimed(bookings, results)
        except Exception as e:
            logger.exception("Batch booking attempt failed for %s", booking_ids)
            for booking in bookings:
                await self._try_notify_unreported_booking(booking, str(e))
            return

        reported: set[str] = set()
        for booking_id, result in results:
            attempted = await self.get_booking(booking_id)
            if attempted is None:
                continue
            reported.add(booking_id)
            await self._try_notify_booking_result(attempted, result)

        # execute_bookings_batch appends a result per request on every path it
        # returns from, so this is a backstop against a booking silently going
        # unanswered rather than an expected branch.
        for booking in bookings:
            if booking.id is not None and booking.id not in reported:
                await self._try_notify_unreported_booking(
                    booking, "The booking attempt ended without reporting a result."
                )

    def _adhoc_execute_at(self) -> datetime | None:
        """When an ad-hoc booking should fire Reserve, or None to fire at once.

        An ad-hoc booking has no window to hit, so any wait here is bought
        deliberately: it is what routes the booking through the same timed path
        the 6:30 race uses - pre-location, clock probing, the precision wait,
        and a session left to age before Reserve goes out. See
        ``walden_adhoc_execute_delay_s``.
        """
        delay_s = settings.walden_adhoc_execute_delay_s
        if delay_s <= 0:
            return None
        execute_at = CTDateTime.to_naive_ct(CTDateTime.now()) + timedelta(seconds=delay_s)
        logger.info(
            "ADHOC_TIMED: Booking on the timed path - Reserve fires at %s CT (in %ds)",
            execute_at.strftime("%H:%M:%S"),
            delay_s,
        )
        return execute_at

    async def _retry_blocked_untimed(
        self,
        bookings: list[TeeTimeBooking],
        results: list[tuple[str, BookingResult]],
    ) -> list[tuple[str, BookingResult]]:
        """Re-attempt refused ad-hoc bookings with no wait, and report the pair.

        The delay above is the only difference between the two attempts, which
        is what makes the pair worth reading: same tee time, same day, same
        code, minutes apart. A refusal that clears on the retry is one the wait
        caused, not another member.

        Only results carrying ``verified_not_reserved`` are retried - a Reserve
        whose outcome could not be established is never sent twice, because a
        second reservation would collide with the club's one-round-per-day rule.

        Never raises. This is a diagnostic control bolted onto a booking that
        has already run and already has an outcome; letting a second browser
        session's failure escape would lose those outcomes and report the
        driver's exception to a member whose timed attempt came back with a
        perfectly readable reason from the club.
        """
        if not settings.walden_adhoc_untimed_retry or settings.walden_adhoc_execute_delay_s <= 0:
            return results

        by_id = {booking.id: booking for booking in bookings if booking.id is not None}
        retryable = [
            by_id[booking_id]
            for booking_id, result in results
            if not result.success and result.verified_not_reserved and booking_id in by_id
        ]
        if not retryable:
            return results

        logger.info(
            "ADHOC_TIMED: %d booking(s) refused on the timed path with nothing reserved; "
            "re-attempting untimed to isolate the wait",
            len(retryable),
        )
        # A fresh session, firing at once - the configuration ad-hoc bookings
        # used before the delay existed, so the retry is the control.
        try:
            retried = dict(await self.execute_bookings_batch(retryable, execute_at=None))
        except Exception:
            logger.exception(
                "ADHOC_TIMED: The untimed retry failed to run; keeping the timed results"
            )
            return results

        merged: list[tuple[str, BookingResult]] = []
        for booking_id, result in results:
            retry = retried.get(booking_id)
            if retry is None:
                merged.append((booking_id, result))
                continue
            logger.info(
                "ADHOC_TIMED: %s - timed attempt refused (%s), untimed retry %s",
                booking_id,
                result.error_message,
                f"booked {retry.booked_time}"
                if retry.success
                else f"also failed: {retry.error_message}",
            )
            merged.append((booking_id, retry))
        return merged

    async def notify_unreported_bookings(self, bookings: list[TeeTimeBooking], error: str) -> None:
        """Report bookings a caller ran but could not account for itself.

        The scheduled job bounds the batch with asyncio.wait_for, so a slow
        morning leaves it holding no results at all. Before this it returned
        from that branch without sending anything, which is how 2026-08-18's
        missed booking reached the member as silence - the one outcome every
        batch path in this module is written to rule out.

        Each row is re-read rather than assumed failed: cancelling the await
        does not stop the Selenium thread behind it, so a booking may have been
        persisted as a success on its way out and must be reported as one.
        """
        for booking in bookings:
            await self._try_notify_unreported_booking(booking, error)

    async def _try_notify_booking_result(
        self, booking: TeeTimeBooking, result: BookingResult
    ) -> None:
        """Report one outcome, keeping a send failure from stranding the batch."""
        try:
            await self._notify_booking_result(booking, result)
        except Exception:
            logger.exception("Could not report the outcome of booking %s", booking.id)

    async def _try_notify_unreported_booking(self, booking: TeeTimeBooking, error: str) -> None:
        """Same, for a booking the batch never accounted for."""
        try:
            await self._notify_unreported_booking(booking, error)
        except Exception:
            logger.exception("Could not report unaccounted booking %s", booking.id)

    async def _notify_unreported_booking(self, booking: TeeTimeBooking, error: str) -> None:
        """Report a booking the batch left unaccounted for.

        The batch may have resolved and persisted the row before failing, so
        this reports what was actually recorded and only invents a failure for
        rows still sitting in IN_PROGRESS.
        """
        persisted = await self.get_booking(booking.id) if booking.id is not None else None
        current = persisted or booking

        if current.status == BookingStatus.SUCCESS:
            await self._notify_booking_result(
                current,
                BookingResult(
                    success=True,
                    booked_time=current.actual_booked_time,
                    confirmation_number=current.confirmation_number,
                ),
            )
            return

        # Only write back a row that is still there to write to; update_booking
        # raises on a missing record, and the message matters more than the row.
        if current.status != BookingStatus.FAILED:
            current.status = BookingStatus.FAILED
            current.error_message = error
            if persisted is not None:
                await database_service.update_booking(current)

        await self._notify_booking_result(
            current,
            BookingResult(success=False, error_message=current.error_message or error),
        )

    async def wait_for_background_bookings(self, timeout: float | None = None) -> None:
        """Wait for all in-flight background booking attempts to finish.

        Used by shutdown and by tests, which need the attempt to complete before
        asserting on its result.

        Args:
            timeout: Seconds to wait before cancelling whatever is still
                running. A booking attempt drives Selenium and can hang, so
                shutdown passes a bound to keep one stuck attempt from blocking
                gateway and provider cleanup. None waits indefinitely.
        """
        while self._background_tasks:
            pending = tuple(self._background_tasks)
            if timeout is None:
                await asyncio.gather(*pending, return_exceptions=True)
                continue

            _, still_running = await asyncio.wait(pending, timeout=timeout)
            if still_running:
                logger.warning(
                    "Cancelling %d booking attempt(s) still running after %ss",
                    len(still_running),
                    timeout,
                )
                for task in still_running:
                    task.cancel()
                await asyncio.gather(*still_running, return_exceptions=True)
            return

    async def get_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Get a booking by its ID.

        Args:
            booking_id: The unique identifier of the booking.

        Returns:
            The booking if found, None otherwise.
        """
        return await database_service.get_booking(booking_id)

    async def get_bookings(
        self, phone_number: str | None = None, status: BookingStatus | None = None
    ) -> list[TeeTimeBooking]:
        """
        Get all bookings, optionally filtered by phone number and/or status.

        Args:
            phone_number: Filter by phone number (optional).
            status: Filter by booking status (optional).

        Returns:
            List of matching bookings.
        """
        return await database_service.get_bookings(phone_number=phone_number, status=status)

    async def cancel_booking(self, booking_id: str) -> TeeTimeBooking | None:
        """
        Cancel a booking by its ID.

        For PENDING/SCHEDULED bookings, this simply updates the status to CANCELLED.
        For SUCCESS bookings (already confirmed on the website), this calls the
        reservation provider to cancel on the actual website.

        Args:
            booking_id: The unique identifier of the booking to cancel.

        Returns:
            The cancelled booking if found and cancellable, None otherwise.
        """
        booking = await database_service.get_booking(booking_id)
        if not booking:
            return None

        if booking.status not in [
            BookingStatus.PENDING,
            BookingStatus.SCHEDULED,
            BookingStatus.SUCCESS,
        ]:
            return None

        if booking.status == BookingStatus.SUCCESS:
            success = await self._cancel_confirmed_booking(booking)
            if not success:
                return None
            return await database_service.get_booking(booking_id)

        booking.status = BookingStatus.CANCELLED
        return await database_service.update_booking(booking)

    def _calculate_execution_time(self, target_date: date) -> datetime:
        """
        Calculate when the booking should be executed.

        Returns a naive datetime representing CT wall-clock time.
        The timezone is used for DST-correct calculation but stripped
        before returning to match the database schema (timestamp without timezone).
        """
        booking_open_date = target_date - timedelta(days=settings.days_in_advance)

        execution_time = datetime.combine(
            booking_open_date,
            datetime.min.time().replace(
                hour=settings.booking_open_hour,
                minute=settings.booking_open_minute,
                second=0,
            ),
        ).replace(tzinfo=CTDateTime.CT_TZ)

        # Return naive datetime (strip timezone) for database storage
        # The value represents CT wall-clock time
        return CTDateTime.to_naive_ct(execution_time)

    def _get_help_message(self) -> str:
        """Return a help message explaining how to use the booking service.

        The text lives in app/services/help_text.py so that the inbound edges,
        which answer a bare mention without ever reaching this service, offer
        the same examples.
        """
        return help_message()

    async def execute_booking(self, booking_id: str) -> bool:
        """
        Execute a scheduled booking by attempting to reserve on the club website.

        This method is called by the scheduler at the reservation open time
        (6:30am CT, 7 days before the requested date). It:
        1. Retrieves the booking record
        2. Updates status to IN_PROGRESS
        3. Calls the reservation provider to book on the club website
        4. Updates status to SUCCESS or FAILED based on result
        5. Sends SMS notification to the user

        Args:
            booking_id: The unique identifier of the booking to execute.

        Returns:
            True if the booking was successful, False otherwise.
        """
        booking = await self.get_booking(booking_id)
        if not booking:
            return False

        # _provider_for resolves this requester's credential from the database
        # and can therefore raise (a DB error, or a decryption failure if
        # CREDENTIAL_ENCRYPTION_KEY changed since the row was written) - unlike
        # the synchronous attribute check this replaced. It runs inside the
        # same try/except as the booking attempt itself so a lookup failure is
        # reported to the user like any other booking failure, instead of
        # escaping to _execute_booking_in_background's bare log-and-swallow.
        provider: ReservationProvider | None = None
        try:
            provider = await self._provider_for(booking.phone_number)
            if not provider:
                booking.status = BookingStatus.FAILED
                booking.error_message = "Reservation provider not configured"
                await database_service.update_booking(booking)

                await self._notify_booking_result(
                    booking,
                    BookingResult(success=False, error_message="System not configured for booking"),
                )
                return False

            booking.status = BookingStatus.IN_PROGRESS
            await database_service.update_booking(booking)

            result = await provider.book_tee_time(
                target_date=booking.request.requested_date,
                target_time=booking.request.requested_time,
                num_players=booking.request.num_players,
                fallback_window_minutes=booking.request.fallback_window_minutes,
            )

            if result.success:
                booking.status = BookingStatus.SUCCESS
                booking.actual_booked_time = result.booked_time
                booking.confirmation_number = result.confirmation_number
                await database_service.update_booking(booking)

                await self._notify_booking_result(booking, result)
                return True
            else:
                booking.status = BookingStatus.FAILED
                booking.error_message = result.error_message
                await database_service.update_booking(booking)

                await self._notify_booking_result(booking, result)
                return False

        except Exception as e:
            booking.status = BookingStatus.FAILED
            booking.error_message = str(e)
            await database_service.update_booking(booking)

            await self._notify_booking_result(
                booking, BookingResult(success=False, error_message=str(e))
            )
            return False
        finally:
            if provider is not None:
                await self._release_provider(provider)

    async def _notify_booking_result(self, booking: TeeTimeBooking, result: BookingResult) -> None:
        """Tell the user how a finished booking attempt turned out.

        Shared by every path that runs an attempt, so an ad-hoc booking reads
        the same to the user whether it ran alone or as part of a batch.
        """
        date_str = booking.request.requested_date.strftime("%A, %B %d")
        num_players = booking.request.num_players

        if result.success:
            time_str = (result.booked_time or booking.request.requested_time).strftime("%I:%M %p")
            details = f"{date_str} at {time_str} for {num_players} players"
            if result.confirmation_number:
                details += f" (Confirmation: {result.confirmation_number})"

            if result.fallback_reason:
                details += f"\n\nNote: {result.fallback_reason}"

            await sms_service.send_booking_confirmation(
                booking.phone_number,
                details,
                booking.origin_channel_id,
                booking.channel,
                requester_handle=booking.requester_handle,
            )
            return

        time_str = booking.request.requested_time.strftime("%I:%M %p")
        booking_details = f"{date_str} at {time_str} for {num_players} players"

        await sms_service.send_booking_failure(
            booking.phone_number,
            result.error_message or "Unknown error",
            result.alternatives,
            booking_details,
            booking.origin_channel_id,
            booking.channel,
            requester_handle=booking.requester_handle,
        )

    async def get_pending_bookings(self) -> list[TeeTimeBooking]:
        return await database_service.get_bookings(status=BookingStatus.SCHEDULED)

    async def get_due_bookings(self, current_time: datetime) -> list[TeeTimeBooking]:
        """
        Get all scheduled bookings that are due for execution.

        A booking is due when its scheduled_execution_time is <= current_time.
        This is used by the Cloud Scheduler job to find bookings to execute.

        The filtering is performed at the database layer for efficiency.
        Timezone handling: scheduled_execution_time is stored as naive datetime
        in CT wall-clock time. We strip tzinfo from current_time to ensure
        consistent naive-to-naive comparison in the database query.

        Args:
            current_time: The current time (timezone-aware in CT) to compare against.

        Returns:
            List of bookings that are due for execution.
        """
        naive_current_time = current_time.replace(tzinfo=None)
        return await database_service.get_due_bookings(naive_current_time)

    async def execute_bookings_batch(
        self,
        bookings: list[TeeTimeBooking],
        execute_at: datetime | None = None,
    ) -> list[tuple[str, BookingResult]]:
        """
        Execute multiple bookings in a batch for efficiency.

        This method groups bookings by date *and requester* and uses the
        provider's batch booking method to book all times for one group in a
        single session. Grouping by requester as well as date matters because
        each requester may have their own Walden login (issue #179): two
        friends wanting different tee times on the same morning must not be
        forced through one shared session, even though one requester's own
        two bookings on the same date still are - which is what preserves the
        efficiency this docstring describes for the common case:
        1. Only one login is required per requester's date group
        2. The driver session is reused for all of that group's bookings
        3. If execute_at is provided, the system logs in early and waits

        Groups run one at a time in this call - concurrent per-requester
        sessions are a follow-up (see _provider_for).

        SMS notifications are NOT sent by this method - the caller is responsible
        for sending notifications after all bookings are complete.

        Args:
            bookings: List of bookings to execute
            execute_at: Optional datetime to wait until before starting bookings.
                       If provided, the provider will log in early and wait until
                       this time before refreshing the page and booking.

        Returns:
            List of (booking_id, BookingResult) tuples for each booking
        """
        if not bookings:
            return []

        if self._reservation_provider is None and self._reservation_provider_factory is None:
            results = []
            for booking in bookings:
                booking_id = booking.id or ""
                booking.status = BookingStatus.FAILED
                booking.error_message = "Reservation provider not configured"
                await database_service.update_booking(booking)
                results.append(
                    (
                        booking_id,
                        BookingResult(
                            success=False,
                            error_message="Reservation provider not configured",
                        ),
                    )
                )
            return results

        groups: dict[tuple[date, str], list[TeeTimeBooking]] = {}
        for booking in bookings:
            key = (booking.request.requested_date, booking.phone_number)
            groups.setdefault(key, []).append(booking)

        all_results: list[tuple[str, BookingResult]] = []

        for (target_date, phone_number), group_bookings in groups.items():
            # _provider_for resolves this requester's credential from the
            # database and can therefore raise: no login on file at all
            # (WaldenCredentialRequiredError, a refusal now rather than a fall
            # back to the shared account), a DB error, or a decryption failure
            # if CREDENTIAL_ENCRYPTION_KEY changed since the row was written.
            # One requester's missing or bad credential must not sink every
            # other requester's bookings in the same batch, so it's caught
            # here, per group, rather than left to escape the loop.
            try:
                # A provider is configured at this point (checked above), so
                # _provider_for either returns one or raises for a requester
                # with no login on file - never None.
                provider = await self._provider_for(phone_number)
                assert provider is not None
            except Exception as e:
                logger.exception(
                    "Could not resolve a Walden provider for requester %s", phone_number
                )
                for booking in group_bookings:
                    booking_id = booking.id or ""
                    booking.status = BookingStatus.FAILED
                    booking.error_message = f"Could not resolve booking credentials: {e}"
                    await database_service.update_booking(booking)
                    all_results.append(
                        (
                            booking_id,
                            BookingResult(success=False, error_message=booking.error_message),
                        )
                    )
                continue

            for booking in group_bookings:
                booking.status = BookingStatus.IN_PROGRESS
                await database_service.update_booking(booking)

            batch_requests = [
                BatchBookingRequest(
                    booking_id=booking.id or "",
                    target_time=booking.request.requested_time,
                    num_players=booking.request.num_players,
                    fallback_window_minutes=booking.request.fallback_window_minutes,
                )
                for booking in group_bookings
            ]

            try:
                batch_result = await provider.book_multiple_tee_times(
                    target_date=target_date,
                    requests=batch_requests,
                    execute_at=execute_at,
                )
            finally:
                await self._release_provider(provider)

            booking_map = {b.id: b for b in group_bookings if b.id is not None}
            for item_result in batch_result.results:
                booking_opt = booking_map.get(item_result.booking_id)
                if not booking_opt:
                    continue

                result = item_result.result
                if result.success:
                    booking_opt.status = BookingStatus.SUCCESS
                    booking_opt.actual_booked_time = result.booked_time
                    booking_opt.confirmation_number = result.confirmation_number
                    all_results.append((item_result.booking_id, result))
                else:
                    booking_opt.status = BookingStatus.FAILED
                    booking_opt.error_message = result.error_message
                    all_results.append((item_result.booking_id, result))

                await database_service.update_booking(booking_opt)

        return all_results


booking_service = BookingService()
