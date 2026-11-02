# Feature manifest

One row per feature: what it means to a user, its status, the paths that implement it, and the date the
status was decided. The tech-debt Routine judges `td:legacy-feature` against this
file (`operations/routines/tech-debt.md`, "Legacy features need a manifest"). It
never decides on its own that a feature is abandoned.

**The maintainer owns the Status column.** This file was first drafted by a
session from the code and the terraform defaults (#287). The *Decided* column is the
date the maintainer confirmed the row: 2026-10-15 for Twilio and Discord, and
2026-10-18 for every `live` row. A new row a session adds says `proposed` there until
the maintainer dates it, and the Routine should treat `proposed` as a draft, not a
decision.

| Status | What the Routine does with it |
|---|---|
| `live` | nothing; never a finding |
| `experimental` | nothing; never a finding |
| `deprecated` | files an issue if its paths still exist; no fix |
| `removable` | eligible for a removal PR |
| `planned` | code for it is a question, not a finding |
| `removed` | lives in the Removed table, not the feature tables; files an issue if code reappears at its paths |

No feature row below is `removable`. Two are `deprecated` (decided 2026-10-15), and what
would make them `removable` is written in their notes.

## How the draft was made, and what it does not establish

Established, by reading `app/` and `terraform/` on 2026-10-15:

- which modules are imported from where (a grep of `import` statements)
- which terraform variables gate a feature, and their defaults
- which routes `app/main.py` registers

**Not established by the draft:** anything about production use. No log was read when
it was written; the maintainer's 2026-10-18 dates confirm the statuses, and do not
add evidence to the notes below. "Live" means *wired in and on by default*, not
*exercised last week*. The two features
marked `deprecated` rest on the terraform default
(`messaging_channel = "telegram"`) and from open issue #178; whether Discord or
Twilio is still switched on in the deployed service is unverified, and the
deployed value can differ from the default (`CLAUDE.md`, "Verify an apply").

## Booking

| Feature | Why a user cares | Status | Paths | Decided | Notes |
|---|---|---|---|---|---|
| Direct-HTTP booking chain | Fast enough to win the 6:30 race: the booking request is sent directly instead of by clicking through the site, so the tee time is more likely to be yours. | `live` | `app/providers/walden_http_booker.py`, `app/providers/walden_http.py`, `app/providers/walden_http_login.py`, `app/utils/cpu_telemetry.py` | 2026-10-18 | `walden_direct_http_booking` defaults on. The racer's critical path |
| Browser-driven Walden provider | The slower click-through route the bot falls back on if the fast one fails before it reserves, and the part that logs in and finds the slot. Fewer missed bookings. | `live` | `app/providers/walden_provider.py`, `app/providers/walden_date_selection.py`, `app/providers/walden_dom_schema.py`, `app/providers/walden_sheet_grid.py`, `app/providers/wait_helper.py` | 2026-10-18 | Not dead beside the HTTP booker: per the `walden_direct_http_booking` description, login, navigation and slot discovery still run in Chrome, and a failure before Reserve is sent falls back to the JavaScript chain. The observer also imports the sheet and DOM modules |
| Mock reservation provider | Nothing directly. It lets the bot be developed and tested without booking real tee times. | `live` | `MockWaldenProvider` in `app/providers/walden_provider.py`, `app/providers/setup.py` | 2026-10-18 | Installed when Walden credentials are absent: local development and tests. Not a production path |
| Booking racer (06:28 fan-out job) | Each member's request is raced in its own container at 06:28, so one member's booking cannot slow down another's. | `live` | `app/racer/`, `terraform/racer.tf` | 2026-10-18 | `teetime-racer` Cloud Run job |
| Tee-sheet observer | Photographs the club's tee sheet each morning, which feeds the check that a time you ask for really exists. | `live` | `app/observer/`, `terraform/observer.tf` | 2026-10-18 | `teetime-observer` Cloud Run job; `observer_enabled` is the switch |
| Slot agreement (#216) | Before the race, the bot offers the real tee times nearest your request and you pick one, so you agree to a time that can actually be booked. | `live` | `app/services/slot_agreement.py` | 2026-10-18 | Shipped as #231 and #232 |
| Booking service and REST API | The core of the bot: takes your request, remembers it, books it at the right moment and tells you the outcome. | `live` | `app/services/booking_service.py`, `app/services/database_service.py`, `app/api/bookings.py`, `app/api/jobs.py`, `app/api/health.py`, `app/models/` | 2026-10-18 | Whether anything calls the REST `bookings` routes directly, as opposed to the messaging path, is not verified |
| Admin proxy booking | The admin can book on a member's behalf, using that member's own stored login, for testing purposes and general help. | `live` | `app/services/proxy_booking.py` | 2026-10-18 | `admin_proxy_enabled` defaults on |

## Members and credentials

| Feature | Why a user cares | Status | Paths | Decided | Notes |
|---|---|---|---|---|---|
| Per-member Walden logins | Each member books under their own club membership, so a round is booked in your name, not the admin's. | `live` | `app/services/credential_service.py`, `app/services/credential_crypto.py` | 2026-10-18 | `credential_store_enabled` defaults on (#179) |
| KMS login encryption | Your stored club login is encrypted with a Google-managed key, and every use of that key is audit-logged. | `live` | `app/services/credential_crypto.py`, `terraform/kms.tf` | 2026-10-18 | `credential_kms_enabled` on since 2026-10-03. The Fernet path in the same file is still read for logins not yet re-entered, and is on its way out (`operations/credential-encryption.md`, PR #271): that is a change inside this feature, not a separate one |
| Self-service onboarding | You connect your own club login on a private page and the admin never sees it. Anyone in the members' Telegram group can use the bot, and leaving the group deletes your login. | `live` | `app/api/onboarding.py`, `app/services/member_setup.py`, `app/services/telegram_members.py` | 2026-10-18 | Members group access, `telegram_group_access_enabled` on since 2026-10-03 |
| Member pseudonyms and log safety | Your name stays out of logs and the public repository; you appear as a label such as Member A. After `/forget`, your booking history is kept under that label instead of your account. | `live` | `app/services/pseudonyms.py`, `app/log_safety.py` | 2026-10-18 |  |
| Data retention | Old conversations and stale logins are deleted on a schedule, and a member who sent `/forget` has their finished bookings moved under their label each night. | `live` | `app/services/retention.py` | 2026-10-18 | Reached from `app/api/jobs.py` (`/purge-expired`) |

## Messaging

| Feature | Why a user cares | Status | Paths | Decided | Notes |
|---|---|---|---|---|---|
| Telegram channel | How you talk to the bot: message it in plain language to book a tee time and get the result back in the same chat. | `live` | `app/providers/telegram_provider.py`, `app/services/telegram_members.py`, `/webhooks/telegram` in `app/api/webhooks.py` | 2026-10-18 | `messaging_channel` and `telegram_enabled` both default to Telegram / on |
| Message parsing (Gemini) | Lets you write what you want in plain language, such as a day and a time, instead of filling in a form. | `live` | `app/services/gemini_service.py`, `app/services/strict_parser.py` | 2026-10-18 | Called from `booking_service.py` for every incoming message. `strict_parser.py` reads every message first and accepts only a booking that names an explicit date and time (the 13 phrasings members used in October 2026); Gemini gets only what it refuses. A time without am/pm is read within tee-sheet hours, 7:00 am to 6:00 pm. `GEMINI_API_KEY` is a standing secret. Uses the `google-genai` SDK (1.x), which replaced the deprecated `google-generativeai` in #133 |
| Shared message layer | Makes sure each reply goes back on the channel you used, and holds the help text the bot shows you. | `live` | `app/services/sms_service.py`, `app/providers/sms_base.py`, `app/services/help_text.py` | 2026-10-18 | **Not Twilio-specific, and not removable with Twilio.** `sms_service` is the per-channel router that Telegram goes through, and `telegram_provider.py` imports `sms_base`. The names are historical |
| Twilio SMS / WhatsApp | Text-message access to the bot. Being retired; Telegram replaces it. | `deprecated` | `app/providers/twilio_provider.py`, `/webhooks/twilio/sms` and `/webhooks/twilio/status` in `app/api/webhooks.py`, `twilio_*` in `app/config.py`, the Twilio secrets in `terraform/main.tf` | 2026-10-15 | #178 asks to remove it. **Before marking `removable`:** `MockSMSProvider` lives in `twilio_provider.py` and `sms_service` falls back to it, so it has to move first; `messaging_channel = "twilio"` is still a valid terraform value and is the Python default in `app/config.py`; `observer_phone_number` and `member_setup.py` may assume phone numbers. None of this was traced to the end |
| Discord channel | Discord access to the bot. Being retired; Telegram replaces it. | `deprecated` | `app/providers/discord_provider.py`, `app/services/discord_gateway.py`, the gateway branch in `app/main.py` | 2026-10-15 | Dormant under the Telegram default: the gateway starts only when `messaging_channel = "discord"`. Re-enabling it forces `min/max_instances = 1` and always-on CPU (~$58/month, #168). `DISCORD_*` secrets are still created by terraform. Your call whether this is retired or a fallback you want kept |

## Core

Shared by every feature above. A file that belongs to all of them is listed here so
that it does not have to be claimed by one.

| Feature | Why a user cares | Status | Paths | Decided | Notes |
|---|---|---|---|---|---|
| Application shell and settings | Nothing directly. It is what runs everything else. | `live` | `app/main.py`, `app/config.py`, `app/models/schemas.py` | 2026-10-18 | FastAPI app, lifespan and the `Settings` class. `app/config.py` also carries the `twilio_*` and Discord settings named in their rows |
| Reservation-provider interface | Nothing directly. It keeps the booking code swappable for testing. | `live` | `app/providers/base.py` | 2026-10-18 |  |
| Time-zone helpers | Makes 6:30 mean 6:30 in Central time, including across daylight saving. | `live` | `app/utils/timezone.py` | 2026-10-18 |  |

## Removed

A feature moves here when its code is deleted: the removal PR moves the row out of
the tables above and into this one, so the decision survives the deletion. Nothing
has been removed yet.

The paths here are where the code *was*. They are exempt from the "manifest path no
longer exists" check, and the opposite check applies instead: code reappearing at
one of them is a question for the maintainer, not a finding that it is dead.

| Feature | Removed | PR | Why | Paths it had |
|---|---|---|---|---|

## Not in this table

Everything under `scripts/`, `tests/` and `operations/` is outside the manifest, and
so is `terraform/` except where a row names a file there as part of a feature.

The tables are checked by `tests/test_feature_manifest.py`: every `app/` and
`terraform/` path a row names must exist, and every `app/**/*.py` file must be named
by a row or sit under a directory a row names. A PR that adds, retires or deletes a
feature updates this file in the same change, including a "Why a user cares" cell
written for a member, not a developer (`CLAUDE.md`, "Operations"). A feature with no
user-visible effect says so in plain words.
