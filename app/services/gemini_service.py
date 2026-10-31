import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.config import settings
from app.models.schemas import ParsedIntent, TeeTimeRequest
from app.services.sms_service import sms_service
from app.services.strict_parser import EARLIEST_TEE_TIME, LATEST_TEE_TIME, strict_parse

logger = logging.getLogger(__name__)

# Failures that are worth a second attempt: quota and rate limits, Google-side
# overload, and timeouts. Everything else (a bad key, a retired model, a
# malformed request) fails the same way on every attempt, so retrying it only
# adds latency before the same error.
#
# Without this, a single blip surfaced to the user as "my language model is
# unavailable" with nothing booked - which is exactly what happened to a 'Yes'
# confirming two tee times, one minute after the same model had parsed the
# booking request successfully.
#
# The google-genai SDK raises one APIError type for every HTTP failure and
# carries the status in .code, so "transient" is decided by code (issue #133;
# the old google-generativeai SDK had an exception class per status):
#   429 quota / rate limit, 500 internal, 503 overloaded, 504 deadline.
# Network failures and timeouts reach us as httpx exceptions, since the SDK's
# async client is httpx-based when aiohttp is not installed.
TRANSIENT_STATUS_CODES = frozenset({429, 500, 503, 504})
TRANSIENT_API_EXCEPTIONS = (
    genai_errors.APIError,  # narrowed to TRANSIENT_STATUS_CODES by is_transient
    httpx.TimeoutException,
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
)

# A 429 is not always a rate limit. On 2026-10-28 every request failed with
# "429 Your prepayment credits are depleted", which no retry can fix: it lasts
# until someone tops up the AI Studio balance, and until then members were told
# to "try again in a minute". These fragments mark that case, matched against
# the error text because the SDK raises the same ResourceExhausted for both.
BILLING_EXHAUSTED_MARKERS = ("prepayment credits", "credits are depleted")

# How often the admin is told about a depleted balance. One message per member
# request would flood their chat during exactly the outage they already know
# about; once an hour is a reminder that it is still down.
BILLING_ALERT_INTERVAL = 3600.0

# Three attempts with 0.5s then 1.0s of backoff, enough to ride out the
# sub-second failures that make up most of them.
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRY_BASE_DELAY = 0.5

# The whole parse, retries and backoff included, is bounded - not just the
# sleeps between attempts. The Twilio webhook at app/api/webhooks.py:59 awaits
# this call and then sends the reply before returning, and Twilio drops the
# request after ~15s, so a budget built only from backoff delays would be
# fiction: three slow-but-not-failing calls could blow through it while every
# attempt "succeeds". Each attempt also carries its own provider-side deadline,
# so a single hung request cannot eat the entire budget.
GEMINI_REQUEST_TIMEOUT = 6.0
GEMINI_TOTAL_BUDGET = 10.0


def is_transient(error: BaseException) -> bool:
    """True when a second attempt could plausibly succeed."""
    if isinstance(error, genai_errors.APIError):
        return error.code in TRANSIENT_STATUS_CODES
    return isinstance(error, TRANSIENT_API_EXCEPTIONS)


def is_billing_exhausted(error: BaseException) -> bool:
    """True when the API refused because the account has no credit left."""
    if not isinstance(error, genai_errors.APIError) or error.code != 429:
        return False
    text = str(error).lower()
    return any(marker in text for marker in BILLING_EXHAUSTED_MARKERS)


SYSTEM_PROMPT = """You are a helpful assistant for booking golf tee times at Northgate Country Club.
Your job is to understand the user's intent and extract structured information from their messages.

The user can:
1. Book a new tee time (provide date, time, number of players)
2. Check the status of their bookings
3. Cancel a booking
4. Modify an existing booking
5. Ask for help

When parsing booking requests:
- Dates can be relative (e.g., "Saturday", "next week", "tomorrow") or absolute (e.g., "December 20", "12/23")
- Times can range from early morning to late afternoon (e.g., "8am", "7:30", "2pm", "5:30pm")
- Tee times run from 7:00am to 6:00pm, so a time without am/pm has only one reading: 7 to 11 are morning, 12 and 1 to 6 are afternoon (e.g., "9:30" is 9:30am, "1:15" is 1:15pm). Never ask whether such a time is am or pm.
- If a time given with am/pm falls outside 7:00am to 6:00pm (e.g., "12:08 am"), do not book it as written and do not guess: set intent to "unclear" and ask for the time in response_message.
- Number of players defaults to 4 if not specified
- "Same as last week" means repeat the previous booking

IMPORTANT: When the user provides a date without a year (like "12/23" or "December 30"), you MUST use the current year provided in the context. If that date has already passed this year, use next year. Always return dates in YYYY-MM-DD format with the correct year.

Always be friendly and confirm details before booking.
If information is missing, ask for clarification.
"""

FUNCTION_DECLARATIONS = [
    {
        "name": "parse_tee_time_request",
        "description": "Parse a user's request to book golf tee times. Supports multiple bookings in a single message.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["book", "status", "cancel", "modify", "help", "confirm", "unclear"],
                    "description": "The user's intent",
                },
                "bookings": {
                    "type": "array",
                    "description": "List of tee time bookings requested. Use this when user requests multiple bookings in one message.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requested_date": {
                                "type": "string",
                                "description": "The requested date in YYYY-MM-DD format",
                            },
                            "requested_time": {
                                "type": "string",
                                "description": "The requested time in HH:MM format (24-hour)",
                            },
                            "num_players": {
                                "type": "integer",
                                "description": "Number of players (1-4)",
                            },
                        },
                        "required": ["requested_date", "requested_time"],
                    },
                },
                "requested_date": {
                    "type": "string",
                    "description": "The requested date in YYYY-MM-DD format (for single booking, deprecated - use bookings array)",
                },
                "requested_time": {
                    "type": "string",
                    "description": "The requested time in HH:MM format (24-hour) (for single booking, deprecated - use bookings array)",
                },
                "num_players": {
                    "type": "integer",
                    "description": "Number of players (1-4) (for single booking, deprecated - use bookings array)",
                },
                "clarification_needed": {
                    "type": "string",
                    "description": "Question to ask if information is missing",
                },
                "response_message": {
                    "type": "string",
                    "description": "A friendly response message to send to the user",
                },
            },
            "required": ["intent", "response_message"],
        },
    }
]


class GeminiService:
    def __init__(self) -> None:
        self._client: genai.Client | None = None
        # monotonic() time of the last depleted-balance alert to the admin.
        # In memory only: max_instances is 1, and a restart re-alerting once
        # is harmless.
        self._last_billing_alert: float | None = None

    @property
    def client(self) -> genai.Client | None:
        """The Gemini client, or None when GEMINI_API_KEY is unset."""
        if self._client is None and settings.gemini_api_key:
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    @staticmethod
    def _request_config(timeout_s: float) -> genai_types.GenerateContentConfig:
        """Model instructions and tools, plus this attempt's deadline.

        The old SDK bound these to a GenerativeModel once; google-genai takes
        them per call. The timeout is in milliseconds.
        """
        return genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[
                genai_types.Tool(
                    function_declarations=[
                        genai_types.FunctionDeclaration.model_validate(declaration)
                        for declaration in FUNCTION_DECLARATIONS
                    ]
                )
            ],
            http_options=genai_types.HttpOptions(timeout=max(1, int(timeout_s * 1000))),
        )

    async def _generate_with_retry(self, prompt: str) -> genai_types.GenerateContentResponse:
        """Call the model, retrying transient failures with exponential backoff.

        Uses the SDK's async method rather than the blocking one: this runs on
        the same event loop that serves every other request, so a slow Gemini
        call must not stall the process - and retrying a blocking call would
        stall it up to three times over.

        Only errors is_transient accepts are retried. Anything
        else propagates on the first attempt, as does a transient error that
        survives the last one - the caller turns both into the honest "nothing
        was booked" reply rather than a guess.

        Attempts stop early once GEMINI_TOTAL_BUDGET is spent, so a run of slow
        responses cannot outlast the webhook waiting on it.
        """
        client = self.client
        if client is None:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        deadline = monotonic() + GEMINI_TOTAL_BUDGET
        last_error: Exception | None = None

        for attempt in range(GEMINI_MAX_ATTEMPTS):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break

            try:
                return await client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=prompt,
                    config=self._request_config(min(GEMINI_REQUEST_TIMEOUT, remaining)),
                )
            except TRANSIENT_API_EXCEPTIONS as e:
                if not is_transient(e) or is_billing_exhausted(e):
                    raise
                last_error = e
                delay = GEMINI_RETRY_BASE_DELAY * (2**attempt)
                if attempt == GEMINI_MAX_ATTEMPTS - 1 or monotonic() + delay >= deadline:
                    break
                logger.warning(
                    "Transient Gemini failure (attempt %d/%d): %s: %s. Retrying in %.1fs",
                    attempt + 1,
                    GEMINI_MAX_ATTEMPTS,
                    type(e).__name__,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error

        # Budget gone before a single attempt could start.
        raise TimeoutError(
            f"Gemini budget of {GEMINI_TOTAL_BUDGET}s exhausted before any attempt completed"
        )

    def _resolve_relative_date(self, date_str: str) -> date | None:
        today = datetime.now().date()
        date_str_lower = date_str.lower()

        if date_str_lower == "today":
            return today
        elif date_str_lower == "tomorrow":
            return today + timedelta(days=1)
        elif date_str_lower in [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]:
            days = [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ]
            target_day = days.index(date_str_lower)
            current_day = today.weekday()
            days_ahead = target_day - current_day
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)

        # Try ISO format first (YYYY-MM-DD)
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

        # Try MM/DD/YYYY and MM/DD formats
        current_year = datetime.now().year
        for fmt in ("%m/%d/%Y", "%m/%d"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                if fmt == "%m/%d":
                    # No year provided, use current year
                    parsed = parsed.replace(year=current_year)
                return parsed.date()
            except ValueError:
                pass

        # Try "Month DD" and "Month DD, YYYY" formats
        for fmt in ("%B %d", "%B %d, %Y", "%b %d", "%b %d, %Y"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=current_year)
                return parsed.date()
            except ValueError:
                pass

        logger.warning(f"Failed to parse date: '{date_str}'")
        return None

    def _parse_time(self, time_str: str) -> time | None:
        """Parse time string in various formats, within tee-sheet hours.

        Supports:
        - 24-hour: "08:58", "14:30", "8:58"
        - 24-hour with seconds: "08:58:00"
        - 12-hour with AM/PM: "8:58 AM", "8:58AM", "8:58a", "2:30 PM", "2:30pm"

        Tee times run 7:00 am to 6:00 pm (EARLIEST_TEE_TIME/LATEST_TEE_TIME,
        shared with strict_parser), so a time without am/pm whose hour is 1-6
        is the afternoon, and any time outside those hours is None. The prompt
        tells Gemini the same; this holds it to that whatever the model returns.
        """
        if not time_str:
            return None

        time_str = time_str.strip()
        parsed: time | None = None

        # Try 24-hour formats first
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                parsed = datetime.strptime(time_str, fmt).time()
            except ValueError:
                continue
            # An hour of 1-6 without am/pm, padded or not ("1:15", "01:15"), is
            # the afternoon: the morning reading is never a tee time (the
            # maintainer's rule, 2026-10-29).
            if 1 <= parsed.hour <= 6:
                parsed = parsed.replace(hour=parsed.hour + 12)
            break

        if parsed is None:
            # Try 12-hour formats with AM/PM
            # Normalize: "8:58a" -> "8:58 AM", "8:58AM" -> "8:58 AM", "8:58 am" -> "8:58 AM"
            normalized = time_str.upper()
            # Handle "8:58a" or "8:58p" (single letter suffix)
            normalized = re.sub(r"(\d)A$", r"\1 AM", normalized)
            normalized = re.sub(r"(\d)P$", r"\1 PM", normalized)
            # Handle "8:58AM" or "8:58PM" (no space before AM/PM)
            normalized = re.sub(r"(\d)(AM|PM)$", r"\1 \2", normalized)

            for fmt in ("%I:%M %p", "%I:%M:%S %p"):
                try:
                    parsed = datetime.strptime(normalized, fmt).time()
                    break
                except ValueError:
                    pass

        if parsed is None:
            logger.warning(f"Failed to parse time: '{time_str}'")
            return None
        if not EARLIEST_TEE_TIME <= parsed <= LATEST_TEE_TIME:
            logger.warning(f"Time outside tee-sheet hours: '{time_str}' -> {parsed}")
            return None
        return parsed

    async def parse_message(self, message: str, context: str | None = None) -> ParsedIntent:
        # A plain "book 11/6 at 9:38a for 4" needs no model to read, so the
        # strict parser goes first and Gemini only sees what it refuses. It
        # accepts only explicit, unambiguous booking commands and never
        # guesses (app/services/strict_parser.py); the request still goes
        # through the same echo-and-confirm flow as a Gemini parse.
        #
        # Only outside a conversation. With context (a slot choice, a pending
        # request) the reply means something relative to it - Gemini sees the
        # pending party size, the strict parser defaults to 4 and would turn a
        # pick into a new request - so Gemini reads it. If Gemini then fails,
        # the member gets the outage reply and the pending state is kept;
        # the strict parser is not a fallback there for the same reason.
        if context is None:
            strict = strict_parse(message)
            if strict is not None:
                logger.info("Read the request with strict_parse; Gemini not called")
                return strict

        if not self.client:
            # Distinct from the API-error path below: this only fires when
            # GEMINI_API_KEY itself is unset (the local-dev-without-a-key case).
            # In deployed environments the key is a required Secret Manager
            # mount, so reaching here means that's missing - log loudly so a
            # misconfiguration doesn't silently guess bookings the way the
            # dead-model fallback used to.
            logger.warning(
                "GEMINI_API_KEY not configured; using _mock_parse (guessed dates/times) "
                "for message: %r",
                message,
            )
            return self._mock_parse(message)

        try:
            # Always include the current date so Gemini knows what year it is
            today = datetime.now()
            date_context = (
                f"Today's date is {today.strftime('%A, %B %d, %Y')} ({today.strftime('%Y-%m-%d')})."
            )

            prompt = f"{date_context}\n\n"
            if context:
                prompt += f"Previous context: {context}\n\n"
            prompt += f"User message: {message}"

            response = await self._generate_with_retry(prompt)

            content = response.candidates[0].content if response.candidates else None
            if content is not None and content.parts:
                for part in content.parts:
                    # Every google-genai Part has a function_call attribute; it
                    # is None on a text part, so test the value, not hasattr.
                    fc = part.function_call
                    if fc is not None:
                        # google-genai returns args as plain JSON types (dicts,
                        # lists, str, numbers) with the declared snake_case keys,
                        # so the old protobuf conversion is not needed (#133).
                        args = dict(fc.args or {})
                        logger.info(f"Gemini parsed args: {args}")
                        return self._build_parsed_intent(args, raw_message=message)

            return ParsedIntent(
                intent="unclear",
                raw_message=message,
                response_message="I'm not sure I understood that. Could you please rephrase?",
            )

        except Exception as e:
            logger.exception(f"Gemini API error for message '{message}': {e}")
            if is_billing_exhausted(e):
                told = await self._alert_admin_billing_exhausted()
                return ParsedIntent(
                    intent="unclear",
                    raw_message=message,
                    parser_failed=True,
                    response_message=(
                        "I can't read messages right now - my language model account "
                        "has run out of credit, so nothing was booked. "
                        + ("The admin has been told; please " if told else "Please ")
                        + "send your request again once it's fixed."
                    ),
                )
            # Do NOT fall back to _mock_parse here. The mock ignores the date and
            # time in the message and defaults to "7 days out at 8:00 AM", so a
            # dead model turned "book 8/2 at 5:06p" into a confident confirmation
            # of a booking the user never asked for. Failing loudly is the only
            # safe answer when we cannot understand the request.
            return ParsedIntent(
                intent="unclear",
                raw_message=message,
                parser_failed=True,
                response_message=(
                    "I couldn't understand that - my language model is unavailable "
                    "right now, so nothing was booked. Please try again in a minute."
                ),
            )

    async def _alert_admin_billing_exhausted(self) -> bool:
        """Tell the admin the Gemini balance is empty; True if they have been told.

        Throttled to once per BILLING_ALERT_INTERVAL. A send that fails is not
        counted, so the next request tries again. An ordinary send failure
        never raises, so the member still gets their reply if Telegram is down
        too; a cancellation propagates after the claim is released.
        """
        admin = settings.telegram_admin_id()
        if not admin:
            return False
        now = monotonic()
        if (
            self._last_billing_alert is not None
            and now - self._last_billing_alert < BILLING_ALERT_INTERVAL
        ):
            return True
        # Claim the slot before awaiting the send, so a second request arriving
        # while it is in flight does not send a duplicate. Released on failure.
        previous = self._last_billing_alert
        self._last_billing_alert = now
        try:
            sent = await sms_service.send_sms(
                admin,
                "Gemini refused a member's message: the prepaid credit is used up. Until "
                "it is topped up at https://ai.studio/projects, only plain requests with "
                'an explicit date and time ("book 11/6 at 9:38am") can be read; members '
                "who send anything else are told nothing was booked.",
                channel="telegram",
            )
        except asyncio.CancelledError:
            # Not an Exception: release the claim, or later requests would
            # skip the alert for an hour while saying the admin was told.
            self._last_billing_alert = previous
            raise
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("Could not alert the admin about the Gemini balance")
            self._last_billing_alert = previous
            return False
        if sent is None:
            logger.error("Could not alert the admin about the Gemini balance")
            self._last_billing_alert = previous
            return False
        return True

    def _build_parsed_intent(
        self, args: dict[str, Any], raw_message: str | None = None
    ) -> ParsedIntent:
        intent = args.get("intent", "unclear")
        tee_time_request = None
        tee_time_requests: list[TeeTimeRequest] | None = None

        if intent == "book":
            bookings = args.get("bookings", [])
            if bookings and isinstance(bookings, list) and len(bookings) > 0:
                tee_time_requests = []
                for booking in bookings:
                    raw_date = booking.get("requested_date", "")
                    raw_time = booking.get("requested_time", "")
                    resolved_date = self._resolve_relative_date(raw_date)
                    parsed_time = self._parse_time(raw_time)

                    if not resolved_date or not parsed_time:
                        logger.warning(
                            f"Skipping booking due to parse failure: "
                            f"date='{raw_date}' -> {resolved_date}, "
                            f"time='{raw_time}' -> {parsed_time}"
                        )
                        continue

                    today = datetime.now().date()
                    if resolved_date < today:
                        try:
                            corrected_date = resolved_date.replace(year=resolved_date.year + 1)
                            resolved_date = corrected_date
                        except ValueError:
                            pass

                    tee_time_requests.append(
                        TeeTimeRequest(
                            requested_date=resolved_date,
                            requested_time=parsed_time,
                            num_players=booking.get("num_players", 4),
                        )
                    )

                if len(tee_time_requests) == 1:
                    tee_time_request = tee_time_requests[0]
                    tee_time_requests = None
                elif len(tee_time_requests) == 0:
                    tee_time_requests = None

            # Fallback to legacy fields if bookings array was empty or all entries failed parsing
            if (
                not tee_time_request
                and not tee_time_requests
                and args.get("requested_date")
                and args.get("requested_time")
            ):
                resolved_date = self._resolve_relative_date(args["requested_date"])
                parsed_time = self._parse_time(args["requested_time"])

                if resolved_date and parsed_time:
                    today = datetime.now().date()
                    if resolved_date < today:
                        try:
                            corrected_date = resolved_date.replace(year=resolved_date.year + 1)
                            resolved_date = corrected_date
                        except ValueError:
                            pass

                    tee_time_request = TeeTimeRequest(
                        requested_date=resolved_date,
                        requested_time=parsed_time,
                        num_players=args.get("num_players", 4),
                    )

        return ParsedIntent(
            intent=intent,
            raw_message=raw_message,
            tee_time_request=tee_time_request,
            tee_time_requests=tee_time_requests,
            clarification_needed=args.get("clarification_needed"),
            response_message=args.get("response_message", ""),
        )

    def _mock_parse(self, message: str) -> ParsedIntent:
        message_lower = message.lower()

        if any(
            word in message_lower for word in ["book", "reserve", "tee time", "saturday", "sunday"]
        ):
            today = datetime.now().date()
            if "saturday" in message_lower:
                days_until_saturday = (5 - today.weekday()) % 7
                if days_until_saturday == 0:
                    days_until_saturday = 7
                target_date = today + timedelta(days=days_until_saturday)
            else:
                target_date = today + timedelta(days=7)

            default_time = time(8, 0)
            num_players = 4

            for word in message_lower.split():
                if "player" in word:
                    try:
                        idx = message_lower.split().index(word)
                        if idx > 0:
                            num_players = int(message_lower.split()[idx - 1])
                    except (ValueError, IndexError):
                        pass

            return ParsedIntent(
                intent="book",
                raw_message=message,
                tee_time_request=TeeTimeRequest(
                    requested_date=target_date,
                    requested_time=default_time,
                    num_players=num_players,
                ),
                response_message=f"Reply yes to confirm: {target_date.strftime('%A, %B %d')} at {default_time.strftime('%I:%M %p')}, {num_players} players.",
            )

        if any(word in message_lower for word in ["status", "booking", "scheduled"]):
            return ParsedIntent(
                intent="status",
                raw_message=message,
                response_message="Let me check your upcoming bookings...",
            )

        if any(word in message_lower for word in ["cancel", "remove", "delete"]):
            return ParsedIntent(
                intent="cancel",
                raw_message=message,
                response_message="Which booking would you like to cancel?",
            )

        if any(word in message_lower for word in ["help", "how", "what"]):
            return ParsedIntent(
                intent="help",
                raw_message=message,
                response_message="I can help you book tee times at Northgate Country Club! Just tell me the date, time, and number of players. For example: 'Book Saturday 8am for 4 players'",
            )

        if message_lower in ["yes", "confirm", "ok", "sure", "yeah"]:
            return ParsedIntent(
                intent="confirm",
                raw_message=message,
                response_message="Great! I'll schedule that booking for you.",
            )

        return ParsedIntent(
            intent="unclear",
            raw_message=message,
            response_message="I'm not sure I understood. You can say things like 'Book Saturday 8am for 4 players' or 'Check my bookings'.",
        )


gemini_service = GeminiService()
