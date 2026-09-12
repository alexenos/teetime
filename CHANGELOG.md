# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A message that only addresses the bot now gets usage help instead of
  silence.** Tagging the bot and sending the request as a separate message —
  `@NorthgateTeetimebot`, then `Book 9/20 at 12p` — used to produce no reply at
  all: the follow-up never reaches the bot (Telegram's group privacy mode does
  not deliver it, and `app/api/webhooks.py` drops unaddressed group text), and
  the bare tag was dropped as having nothing to parse. From the user's side the
  booking request simply went unanswered.

  The bare tag is now answered with what to do differently — keep the request in
  the same message as the tag — followed by example commands, each shown with
  the tag so one can be copied as is. A private chat, and a bot whose own handle
  could not be resolved, get the same reply with untagged examples. The Discord
  gateway answers a bare `@mention` the same way.

  The text lives in the new `app/services/help_text.py`, shared with the
  existing `help` intent so both offer the same examples.

### Changed

- **Every booking now runs under the requester's own Walden login; there is no
  shared account to fall back on.** A requester with no row in
  `walden_credentials` is refused - in the conversation, with "your account
  isn't set up for booking yet" - instead of quietly booking under
  `WALDEN_MEMBER_NUMBER`/`WALDEN_PASSWORD`.

  #179 added that fallback deliberately, so friends could be onboarded one at a
  time without breaking anyone. The cost only became clear once people were
  actually being added: an unconfigured friend booked under somebody else's
  membership with no error and no warning, and because the club allows each
  member one round per day, that booking could spend the slot the real one
  needed. Silence was the wrong default for something that cannot be undone
  from chat.

  Refused at two layers, for the same reason #185's admin guard is:

  - `create_booking` refuses while someone is still reading the reply, so no
    row is written that would look scheduled for a week and then fail at 6:30.
  - `_provider_for` raises `WaldenCredentialRequiredError`, so a row that
    already exists - written before this change, or by any path that skipped
    the first check - still cannot reach the club under an account that is not
    the requester's.

  `ProxyAdminHasNoCredentialError` (#185) is now a subclass of that, since the
  proxy admin was always a special case of the same rule.

  Providers are built per requester from their own login: `app/main.py` installs
  the provider *class* via the new `set_reservation_provider_factory`, rather
  than one instance built from the shared credentials.
  `set_reservation_provider` remains for a single fixed instance -
  `MockWaldenProvider` in local development, and tests - and does **not** bypass
  the credential requirement, which is checked before either mode builds
  anything.

  `CredentialService.resolve()` is replaced by `require_credentials()`, whose
  return type is no longer optional because there is no second choice to
  return.

  **`WALDEN_MEMBER_NUMBER`/`WALDEN_PASSWORD` no longer log anything in.** They
  survive only as the "this is a real deployment, not a laptop" signal that
  picks the real provider class over the mock. Retiring the two secrets is a
  follow-up: dropping them from Terraform's `secrets` list would have Terraform
  delete them from Secret Manager, so that wants its own change.

  **Before deploying:** every user who books must already have a row. Check with
  `poetry run python scripts/add_walden_credential.py list` against the
  production database, and confirm no `SCHEDULED` booking belongs to a requester
  without one - those would fail at 6:30 rather than falling back.

### Added

- **One designated admin account can book on a friend's behalf** (issue #185).
  `TELEGRAM_ADMIN_USER_ID` names exactly one Telegram ID — not a role any
  allowed user can hold — which may write `for @alex book 9/12 at 8a`, or just
  `book 9/12 at 8a` and be asked "for which user?" (a new
  `AWAITING_PROXY_TARGET` conversation state holds the request across that
  turn, the same shape as `AWAITING_CANCELLATION_SELECTION`). The admin must
  still be on `TELEGRAM_ALLOWED_USER_IDS` to reach the bot at all; the app logs
  a warning at startup when it is not.

  This exists so a booking failure can be diagnosed, or a newly added
  credential verified, under the account it actually concerns — rather than
  under whoever happened to send the message, which was the only option before.

  The resulting booking *is* the friend's: it carries their identity, runs
  under their Walden membership, shows up in their history, and its
  confirmation or failure days later goes to their own conversation. The admin
  sees only the immediate acknowledgement, in their own chat.

  Three things fail loudly rather than quietly, because every one of them would
  otherwise book a round under the wrong membership — and with the club's
  one-round-per-member-per-day rule, possibly consume the slot the real booking
  needed:

  - The admin account has **no Walden login of its own** and does not fall back
    to the shared global account. A booking attributed to it is refused when it
    is created, and again in the credential lookup underneath.
  - A target matching **no** stored friend is a question, not a guess.
  - A target matching **more than one** friend (a name colliding with someone
    else's handle) is a question too, naming the candidates.

  `walden_credentials` gains `name` and `telegram_username`, set via
  `scripts/add_walden_credential.py set --name / --telegram-username`, and they
  are what `for @X` matches — case-insensitively, and with the leading `@`
  required, since that sigil is what keeps `for 4 players, book 9/12` a booking
  rather than a friend named "4". Omitting it costs a turn, not the request. `--label` was left as what #183 made it, a free-text note that
  resolves nothing; `list` now flags rows with neither name nor handle, which
  cannot be proxy-booked for. Updating a friend's password no longer clears
  their label, name, or handle.

  Cancelling or checking status on someone else's behalf is not supported yet
  and is refused explicitly rather than applied to the admin's own history.

  Deployment: proxy booking requires `credential_store_enabled = true` — it
  always books under a friend's stored login, so without
  `CREDENTIAL_ENCRYPTION_KEY` mounted every proxy booking would fail at 6:30,
  days after being accepted, when the attempt first tries to decrypt one. A
  Cloud Run precondition enforces that rather than letting it deploy.
  `TELEGRAM_ADMIN_USER_ID` is itself gated behind a new `admin_proxy_enabled`
  Terraform variable, **off by default**, for the same reason
  `credential_store_enabled` is — Terraform creates the secret empty and a
  Cloud Run revision referencing a versionless secret fails to deploy. Create
  the version first, then flip the flag. With it off, nothing about existing
  bookings changes.

### Fixed

- `_run_column_migrations` took its dialect from `settings.database_url` rather
  than from the connection it was handed. Identical in production, where the
  engine is built from that same setting, but it emitted Postgres'
  `ADD COLUMN IF NOT EXISTS` at any other connection passed in — a syntax error
  on SQLite. Found in review of #187.

- **Telegram as a messaging channel, running alongside Discord.** Inbound
  messages arrive as HTTP webhooks (`POST /webhooks/telegram`) rather than over
  a persistent WebSocket, so the service does not need `min-instances=1` and can
  scale to zero between messages. This is the groundwork for retiring the
  always-on Cloud Run instance the Discord gateway requires, which accounts for
  roughly USD 58 of the current USD 71 monthly bill.

  Telegram is enabled by `TELEGRAM_BOT_TOKEN` independently of
  `MESSAGING_CHANNEL`, so both channels can be live at once and Telegram can be
  exercised end to end before Discord is switched off. **No cost is saved until
  that switch happens** - see `docs/telegram-setup.md`.

  In a group the addressing (`@teetimebot`, or `/book@teetimebot`) is stripped
  before the text reaches the parser, mirroring the Discord gateway's
  `strip_bot_mention`. Telegram marks it structurally in the update's
  `entities`, so the removal cuts the marked ranges rather than pattern-matching
  text - a mention of someone else, or a command aimed at a different bot in the
  same group, is left alone. Entity offsets are UTF-16 code units, so an emoji
  earlier in the message shifts them; this app already treats a bare thumbs-up
  as a booking confirmation, so that case is handled rather than assumed away.

  Requests are authenticated with the shared secret Telegram echoes in
  `X-Telegram-Bot-Api-Secret-Token`; an unset secret rejects every update rather
  than trusting the caller. Beyond that, only `TELEGRAM_ALLOWED_USER_IDS` are
  answered.

- **Per-friend Walden Golf credentials, admin-added and encrypted at rest**
  (issue #179, phase 1 of 2). Each friend already has their own Walden
  membership; a new `walden_credentials` table associates a requester's
  existing identity (the same `phone_number` on `SessionRecord`/
  `BookingRecord`) with their own member number and password, added via
  `scripts/add_walden_credential.py` - there is still no self-service
  onboarding, so a credential never transits chat history. Values are
  encrypted with Fernet (`CREDENTIAL_ENCRYPTION_KEY`), not GCP Secret Manager
  API calls, because credential resolution sits on the path to the 6:30 AM
  race and must not add a network round trip there.

  `WaldenGolfProvider`/`MockWaldenProvider` now take credentials as a
  constructor argument instead of reading `settings.walden_member_number`/
  `walden_password` globally, resolving #144's singleton concern via its
  option 3 (a provider per run). `BookingService` resolves each booking's
  requester to their own credential when one exists, falling back to the
  single global account otherwise - existing installs keep working unchanged
  until a friend is actually added. `execute_bookings_batch` now groups by
  date *and* requester rather than date alone, so two friends booking
  different tee times on the same morning no longer share one session.

  Concurrent execution of those per-requester sessions - the other half of
  #179 - is a follow-up: groups still run one at a time in this change, and
  the issue itself flags open questions (Cloud Run resource limits, untested
  club-side behavior under simultaneous logins) that need answering first.

- **Sessions and bookings record the channel they came from.** A booking's
  result notification - including the 6:30 AM confirmation that arrives days
  later - is sent back over the channel it was requested on. Discord and
  Telegram identify users with bare numbers and are otherwise indistinguishable,
  so the recorded channel is the only thing that says which API to answer on.
  Rows written before the column existed have no channel and fall back to
  `MESSAGING_CHANNEL`, exactly as they behaved before.

### Security

- **The Twilio webhook now validates its own signatures.**
  `SMSService.validate_request` delegated to whichever provider
  `MESSAGING_CHANNEL` selected. Discord and Telegram validate inbound requests
  by other means and return `True` from that method, so while
  `MESSAGING_CHANNEL` was `discord` - its default since the Discord channel
  landed - an unsigned POST to the public `/webhooks/twilio/sms` endpoint was
  accepted and could create bookings. The route now names its channel
  explicitly, so Twilio requests are checked against Twilio's validator
  regardless of which channel is configured.

### Fixed

- **Stale pooled database connections no longer kill the morning job.** The
  engine now sets `pool_pre_ping=True` and `pool_recycle=1800` for Postgres, so
  a connection that died during an idle gap is detected and replaced instead of
  being handed to the first query of the day. This is the 2026-08-19 failure in
  the 0.2.0 known issues: the service is idle almost all day, the 06:28 booking
  job is often its only caller between one morning and the next, and Cloud Run
  gives an idle instance no CPU in between. `pool_pre_ping` is what closes the
  failure; recycling is hygiene, so long-lived connections retire on an ordinary
  request rather than on the one request that is racing a clock. SQLite is
  deliberately excluded — it has no connection to go stale, and `:memory:` is
  served by a `StaticPool` where recycling would discard the schema.

- **The opening burst no longer interleaves a fallback ask with the target.**
  `walden_reserve_burst_target_only` now defaults to unset, which
  `walden_burst_target_only()` couples to the burst plan's own length (12
  today) rather than a separately hard-coded number, so every member asks for
  the requested slot and lengthening `walden_reserve_burst_offsets_ms` can
  never silently reintroduce the interleave. The fallback list is walked
  serially afterwards instead, as it already was when the burst granted
  nothing. Found by the 2026-09-04 evening ad-hoc test this mode was built to
  require before a race: a fallback member interleaved into the burst shares
  the target's PrimeFaces ViewState, and when both were granted the club's own
  reservation record ended up anchored to the fallback (04:58 PM) rather than
  the target the chain reported booking (05:06 PM) - the 05:06 PM slot was
  still open on the post-race sheet. See
  `docs/booking-post-mortem-2026-09-04-evening.md`. The interleave code is
  unchanged and stays reachable via an explicit
  `WALDEN_RESERVE_BURST_TARGET_ONLY`, for a future fix that makes concurrent
  grants under one ViewState safe.

## [0.2.0] - 2026-08-20

The release in which the bot started winning the 06:30 race.

Before this work the 6:30 AM booking never beat a human to a contested slot. It
now has three morning wins on record, two of them on the first Reserve it sends,
each verified against the member's own reservations page rather than against a
phrase in the club's response.

### The wins, from the race ledgers

| morning | slot | attempt | sent past window | verdict |
|---|---|---|---|---|
| 2026-08-15 | 05:00 PM | 2 of 2 | +1240ms | accepted — first win ever |
| 2026-08-16 | 08:00 AM | 1 of 1 | +1023ms | accepted — first on the new aim point |
| 2026-08-20 | 08:08 AM | 1 of 1 | +1006ms | accepted — exact slot, 456ms round trip |

The two mornings immediately before the first win (08-13, 08-14) were refused on
a single Reserve sent at −7ms and −14ms; the change that separates them is below.

### Added

- **Direct-HTTP booking path for the race** (#123). Replaces the browser click
  chain at the window with a pre-staged HTTP Reserve, removing the
  Python→Selenium→JS handoff from the critical path.
- **Reserve sweep ladder** (#146). Sends several Reserves for the same slot
  across the opening seconds instead of one, so a single refusal no longer ends
  the morning.
- **Race ledger** (#146). One JSONL row per Reserve — offsets in both our frame
  and the club's, verdict, round trip, response shape — uploaded to GCS on every
  race, win or lose. Every claim in the table above is read from it.
- **Reservation verification** (#150). After a booking reports success, the
  member's reservations page is checked and the result logged as
  `RESERVATION_CHECK`. Closes the failure class where a chain completed against
  no reservation and still looked like a win.
- **Ad-hoc bookings on the fast path** (#126, #145), and **Discord as a
  messaging channel** alongside Twilio (#115, #117).

### Fixed

- **The race is timed from 06:30:01, not 06:30:00** (#150). The club's sheet
  opens a second after its stated window: every Reserve ever sent at ~0ms was
  refused, and every acceptance on record landed in the 06:30:01 second. The aim
  point moved to +1030ms and the clock probe was tightened to bracket the tick
  to ±23ms. This is the change the win record turns on.
- **A held tee time is no longer discarded** (#146). The club ships an inert
  "blocked by another user" popup in responses that *accepted*; reading it as a
  refusal threw away slots the club had already granted.
- **The clock is measured against a page the club answers quickly** (#141), and
  **a Reserve that goes unanswered no longer ends the run** (#149).
- **The slot finder keeps its fallback list** (#142).
- **A timed-out booking is reported to the member** rather than passing silently
  (#154).

### Changed

- Version bumped from the Poetry default `0.1.0`, which was never released.
  `v0.2.0` is the project's first tag, cut against the merge commit that lands
  this entry — so the release link below resolves once that tag exists.

### Known issues

Carried from `docs/booking-post-mortem-2026-08-20.md`, neither fixed in this
release:

- **Stale pooled DB connection.** The engine sets neither `pool_pre_ping` nor
  `pool_recycle`, so a connection dropped during an idle gap is handed out dead
  and the scheduled job dies on its first query. Seen once in 29 invocations
  (2026-08-19), on a morning with nothing scheduled, so it cost nothing — but the
  failure mode is total and there is no retry or alert above it.
  *(Fixed after this release — see Unreleased.)*
- **No alerting on a job that never completes.** Both 2026-08-18 (window missed
  during a 3m58s login) and 2026-08-19 failed silently in the logs.

[0.2.0]: https://github.com/alexenos/teetime/releases/tag/v0.2.0
