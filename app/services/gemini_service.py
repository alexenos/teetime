from datetime import date, datetime, time, timedelta
from typing import Any

import google.generativeai as genai

from app.config import settings
from app.models.schemas import ParsedIntent, TeeTimeRequest

SYSTEM_PROMPT = """You are a helpful assistant for booking golf tee times at Northgate Country Club.
Your job is to understand the user's intent and extract structured information from their messages.

The user can:
1. Book a new tee time (provide date, time, number of players)
2. Check the status of their bookings
3. Cancel a booking
4. Modify an existing booking
5. Ask for help

When parsing booking requests:
- Dates can be relative (e.g., "Saturday", "next week", "tomorrow") or absolute (e.g., "December 20")
- Times can range from early morning to late afternoon (e.g., "8am", "7:30", "2pm", "5:30pm")
- Tee times are available from opening until 5:54pm
- Number of players defaults to 4 if not specified
- "Same as last week" means repeat the previous booking

Always be friendly and confirm details before booking.
If information is missing, ask for clarification.
"""

FUNCTION_DECLARATIONS = [
    {
        "name": "parse_tee_time_request",
        "description": "Parse a user's request to book a golf tee time",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["book", "status", "cancel", "modify", "help", "confirm", "unclear"],
                    "description": "The user's intent",
                },
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
        self._model: Any = None

    @property
    def model(self) -> Any:
        if self._model is None:
            if settings.gemini_api_key:
                genai.configure(api_key=settings.gemini_api_key)
                self._model = genai.GenerativeModel(
                    model_name="gemini-1.5-flash",
                    system_instruction=SYSTEM_PROMPT,
                    tools=[{"function_declarations": FUNCTION_DECLARATIONS}],
                )
            else:
                self._model = None
        return self._model

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

        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _parse_time(self, time_str: str) -> time | None:
        try:
            return datetime.strptime(time_str, "%H:%M").time()
        except ValueError:
            try:
                return datetime.strptime(time_str, "%H:%M:%S").time()
            except ValueError:
                return None

    async def parse_message(self, message: str, context: str | None = None) -> ParsedIntent:
        if not self.model:
            return self._mock_parse(message)

        try:
            prompt = message
            if context:
                prompt = f"Previous context: {context}\n\nUser message: {message}"

            response = self.model.generate_content(prompt)

            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "function_call"):
                        fc = part.function_call
                        args = dict(fc.args)
                        return self._build_parsed_intent(args)

            return ParsedIntent(
                intent="unclear",
                response_message="I'm not sure I understood that. Could you please rephrase?",
            )

        except Exception as e:
            print(f"Gemini API error: {e}")
            return self._mock_parse(message)

    def _build_parsed_intent(self, args: dict) -> ParsedIntent:
        intent = args.get("intent", "unclear")
        tee_time_request = None

        if intent == "book" and args.get("requested_date") and args.get("requested_time"):
            resolved_date = self._resolve_relative_date(args["requested_date"])
            parsed_time = self._parse_time(args["requested_time"])

            if resolved_date and parsed_time:
                tee_time_request = TeeTimeRequest(
                    requested_date=resolved_date,
                    requested_time=parsed_time,
                    num_players=args.get("num_players", 4),
                )

        return ParsedIntent(
            intent=intent,
            tee_time_request=tee_time_request,
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
                tee_time_request=TeeTimeRequest(
                    requested_date=target_date,
                    requested_time=default_time,
                    num_players=num_players,
                ),
                response_message=f"I'll book a tee time for {target_date.strftime('%A, %B %d')} at {default_time.strftime('%I:%M %p')} for {num_players} players. Reply 'yes' to confirm.",
            )

        if any(word in message_lower for word in ["status", "booking", "scheduled"]):
            return ParsedIntent(
                intent="status",
                response_message="Let me check your upcoming bookings...",
            )

        if any(word in message_lower for word in ["cancel", "remove", "delete"]):
            return ParsedIntent(
                intent="cancel",
                response_message="Which booking would you like to cancel?",
            )

        if any(word in message_lower for word in ["help", "how", "what"]):
            return ParsedIntent(
                intent="help",
                response_message="I can help you book tee times at Northgate Country Club! Just tell me the date, time, and number of players. For example: 'Book Saturday 8am for 4 players'",
            )

        if message_lower in ["yes", "confirm", "ok", "sure", "yeah"]:
            return ParsedIntent(
                intent="confirm",
                response_message="Great! I'll schedule that booking for you.",
            )

        return ParsedIntent(
            intent="unclear",
            response_message="I'm not sure I understood. You can say things like 'Book Saturday 8am for 4 players' or 'Check my bookings'.",
        )


gemini_service = GeminiService()
