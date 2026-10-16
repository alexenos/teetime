# Feature manifest

One row per feature: its status, the paths that implement it, and the date the
status was decided. The tech-debt Routine judges `td:legacy-feature` against this
file (`operations/routines/tech-debt.md`, "Legacy features need a manifest"). It
never decides on its own that a feature is abandoned.

**The maintainer owns the Status column.** This file was first drafted by a
session from the code and the terraform defaults (#287). A row whose *Decided*
cell says `proposed` is that draft, not a decision: the Routine should treat it as
the maintainer's word only once a date replaces `proposed`.

| Status | What the Routine does with it |
|---|---|
| `live` | nothing; never a finding |
| `experimental` | nothing; never a finding |
| `deprecated` | files an issue if its paths still exist; no fix |
| `removable` | eligible for a removal PR |
| `planned` | code for it is a question, not a finding |

No row below is `removable`. Two are proposed `deprecated`, and what would make
them `removable` is written in their notes.

## How the draft was made, and what it does not establish

Established, by reading `app/` and `terraform/` on 2026-10-15:

- which modules are imported from where (a grep of `import` statements)
- which terraform variables gate a feature, and their defaults
- which routes `app/main.py` registers

**Not established:** anything about production use. No log was read. "Live" below
means *wired in and on by default*, not *exercised last week*. The two features
proposed `deprecated` are inferred from the terraform default
(`messaging_channel = "telegram"`) and from open issue #178; whether Discord or
Twilio is still switched on in the deployed service is unverified, and the
deployed value can differ from the default (`CLAUDE.md`, "Verify an apply").

## Booking

| Feature | Status | Paths | Decided | Notes |
|---|---|---|---|---|
| Direct-HTTP booking chain | `live` | `app/providers/walden_http_booker.py`, `app/providers/walden_http.py`, `app/providers/walden_http_login.py`, `app/utils/cpu_telemetry.py` | proposed | `walden_direct_http_booking` defaults on. The racer's critical path |
| Browser-driven Walden provider | `live` | `app/providers/walden_provider.py`, `app/providers/walden_date_selection.py`, `app/providers/walden_dom_schema.py`, `app/providers/walden_sheet_grid.py`, `app/providers/wait_helper.py` | proposed | Not dead beside the HTTP booker: per the `walden_direct_http_booking` description, login, navigation and slot discovery still run in Chrome, and a failure before Reserve is sent falls back to the JavaScript chain. The observer also imports the sheet and DOM modules |
| Mock reservation provider | `live` | `MockWaldenProvider` in `app/providers/walden_provider.py`, `app/providers/setup.py` | proposed | Installed when Walden credentials are absent: local development and tests. Not a production path |
| Booking racer (06:28 fan-out job) | `live` | `app/racer/`, `terraform/racer.tf` | proposed | `teetime-racer` Cloud Run job |
| Tee-sheet observer | `live` | `app/observer/`, `terraform/observer.tf` | proposed | `teetime-observer` Cloud Run job; `observer_enabled` is the switch |
| Slot agreement (#216) | `live` | `app/services/slot_agreement.py` | proposed | Shipped as #231 and #232 |
| Booking service and REST API | `live` | `app/services/booking_service.py`, `app/services/database_service.py`, `app/api/bookings.py`, `app/api/jobs.py`, `app/api/health.py`, `app/models/` | proposed | Whether anything calls the REST `bookings` routes directly, as opposed to the messaging path, is not verified |
| Admin proxy booking | `live` | `app/services/proxy_booking.py` | proposed | `admin_proxy_enabled` defaults on |

## Members and credentials

| Feature | Status | Paths | Decided | Notes |
|---|---|---|---|---|
| Per-member Walden logins | `live` | `app/services/credential_service.py`, `app/services/credential_crypto.py` | proposed | `credential_store_enabled` defaults on (#179) |
| KMS login encryption | `live` | `app/services/credential_crypto.py`, `terraform/kms.tf` | proposed | `credential_kms_enabled` on since 2026-10-03. The Fernet path in the same file is still read for logins not yet re-entered, and is on its way out (`operations/credential-encryption.md`, PR #271): that is a change inside this feature, not a separate one |
| Self-service onboarding | `live` | `app/api/onboarding.py`, `app/services/member_setup.py`, `app/services/telegram_members.py` | proposed | Members group access, `telegram_group_access_enabled` on since 2026-10-03 |
| Member pseudonyms and log safety | `live` | `app/services/pseudonyms.py`, `app/log_safety.py` | proposed | |
| Data retention | `live` | `app/services/retention.py` | proposed | Reached from `app/api/jobs.py` (`/purge-expired`) |

## Messaging

| Feature | Status | Paths | Decided | Notes |
|---|---|---|---|---|
| Telegram channel | `live` | `app/providers/telegram_provider.py`, `app/services/telegram_members.py`, `/webhooks/telegram` in `app/api/webhooks.py` | proposed | `messaging_channel` and `telegram_enabled` both default to Telegram / on |
| Message parsing (Gemini) | `live` | `app/services/gemini_service.py` | proposed | Called from `booking_service.py` for every incoming message. `GEMINI_API_KEY` is a standing secret. The `google-generativeai` SDK is deprecated (#133); that is a migration, not a removal |
| Shared message layer | `live` | `app/services/sms_service.py`, `app/providers/sms_base.py`, `app/services/help_text.py` | proposed | **Not Twilio-specific, and not removable with Twilio.** `sms_service` is the per-channel router that Telegram goes through, and `telegram_provider.py` imports `sms_base`. The names are historical |
| Twilio SMS / WhatsApp | `deprecated` | `app/providers/twilio_provider.py`, `/webhooks/twilio/sms` and `/webhooks/twilio/status` in `app/api/webhooks.py`, `twilio_*` in `app/config.py`, the Twilio secrets in `terraform/main.tf` | proposed | #178 asks to remove it. **Before marking `removable`:** `MockSMSProvider` lives in `twilio_provider.py` and `sms_service` falls back to it, so it has to move first; `messaging_channel = "twilio"` is still a valid terraform value and is the Python default in `app/config.py`; `observer_phone_number` and `member_setup.py` may assume phone numbers. None of this was traced to the end |
| Discord channel | `deprecated` | `app/providers/discord_provider.py`, `app/services/discord_gateway.py`, the gateway branch in `app/main.py` | proposed | Dormant under the Telegram default: the gateway starts only when `messaging_channel = "discord"`. Re-enabling it forces `min/max_instances = 1` and always-on CPU (~$58/month, #168). `DISCORD_*` secrets are still created by terraform. Your call whether this is retired or a fallback you want kept |

## Not in this table

Everything under `terraform/`, `scripts/`, `tests/` and `operations/` is outside
the Routine's `app/` check. A row is missing for any `app/` file the tables above
do not name; that is intended, and the Routine will file an issue asking for it to
be classified.

Known gap: `app/models/schemas.py` and `app/config.py` are shared by every
feature and listed only where one feature clearly owns a part of them.
