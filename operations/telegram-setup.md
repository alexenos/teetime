# Telegram Bot Setup

TeeTime can use Telegram as the conversation channel. Unlike Discord, inbound
messages arrive as **HTTP webhooks** rather than over a persistent WebSocket,
so the Cloud Run service does **not** need `min-instances=1` and can scale to
zero between messages. That difference is the whole reason this channel exists:
the always-on instance the Discord gateway requires costs roughly USD 58/month.

Telegram runs **alongside** Discord rather than replacing it. `TELEGRAM_BOT_TOKEN`
turns the channel on independently of `MESSAGING_CHANNEL`, so you can exercise
Telegram end to end while Discord is still the live channel, then switch when
you are satisfied.

> **Note on cost**: enabling Telegram saves nothing on its own. The bill only
> drops once Discord is switched off and `cloud_run_min_instances` goes to 0.

## One-time setup (Telegram side)

1. **Create the bot**: in Telegram, message [@BotFather](https://t.me/BotFather)
   → `/newbot` → give it a name and a username ending in `bot`. BotFather
   replies with the bot token.

2. **Save the token**: treat it like a password — it goes straight into `.env`
   (`TELEGRAM_BOT_TOKEN=...`) or Secret Manager. Never paste it into a chat or
   commit it. Anyone holding it can post as your bot.

3. **Get your user ID**: message [@userinfobot](https://t.me/userinfobot). It
   replies with your numeric ID. This goes in `TELEGRAM_ALLOWED_USER_IDS` and is
   the allowlist — the bot ignores everyone else. It takes a comma-separated
   list, so more people can be added later.

4. **Invent a webhook secret**:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Telegram does
   not sign webhook payloads, so this shared secret is the only thing standing
   between a public endpoint and a forged booking. Without it the endpoint
   rejects every update.

   Telegram constrains the value: 1-256 characters, and only `A-Z`, `a-z`,
   `0-9`, `_` and `-`. `token_urlsafe` stays inside that set; a hand-picked
   passphrase with punctuation does not, and `setWebhook` rejects it with a 400.

5. **Start a chat**: open your bot and send `/start`. A bot cannot message you
   first, so until you do this, outbound notifications have nowhere to go.

## App configuration

```dotenv
TELEGRAM_BOT_TOKEN=<token from step 2>
TELEGRAM_ALLOWED_USER_IDS=<id from step 3>
# Optional: lets this one ID book on a friend's behalf. See "Booking for
# someone else" below. Leave empty to disable.
TELEGRAM_ADMIN_USER_ID=
TELEGRAM_WEBHOOK_SECRET=<secret from step 4>
TELEGRAM_WEBHOOK_BASE_URL=https://<your Cloud Run URL>
```

`TELEGRAM_WEBHOOK_BASE_URL` is what the service registers with Telegram at
startup. Leave it unset locally: there is no public URL to register, outbound
still works, and inbound simply does not run.

Registration is idempotent and re-run on every startup, so a changed service URL
heals itself rather than leaving the bot pointed at a dead endpoint.

## Deploying it

Three steps, in this order. The gate is `telegram_enabled`, **not** the merge:
merging with it off is a no-op for the running service, because the revision
does not reference the Telegram secrets at all until the flag is on.

### 1. Merge and deploy with `telegram_enabled = false` (its default)

Terraform creates the three secrets **empty**. Nothing else changes: the
credentials are not mounted, `TELEGRAM_WEBHOOK_BASE_URL` is empty so no webhook
is registered, and Discord keeps running exactly as before.

This step has to come first because the next one adds *versions* to secrets, and
those secrets do not exist until Terraform has created them.

### 2. Add the secret versions

```bash
PROJECT_ID="teetime"
printf '%s' "<bot token>"     | gcloud secrets versions add TELEGRAM_BOT_TOKEN --data-file=- --project=$PROJECT_ID
printf '%s' "<your user id>"  | gcloud secrets versions add TELEGRAM_ALLOWED_USER_IDS --data-file=- --project=$PROJECT_ID
# Only if you want proxy booking (see "Booking for someone else"); also set
# admin_proxy_enabled = true in terraform, AFTER adding this version.
printf '%s' "<your user id>"  | gcloud secrets versions add TELEGRAM_ADMIN_USER_ID --data-file=- --project=$PROJECT_ID
printf '%s' "<webhook secret>"| gcloud secrets versions add TELEGRAM_WEBHOOK_SECRET --data-file=- --project=$PROJECT_ID
```

### 3. Turn the channel on

Change the **default** of `telegram_enabled` to `true` in
`terraform/variables.tf`, and deploy. It has to be the default rather than a
`terraform.tfvars` entry: `*.tfvars` is gitignored, so it is absent from the
Cloud Build checkout, and `cloudbuild.yaml` passes only `project_id`, `region`,
`container_image` and `log_level`. Every other variable resolves to its default.
`TELEGRAM_WEBHOOK_BASE_URL` is filled in from the service's own URL
automatically.

A Cloud Run revision that references a secret with no version fails to deploy,
which is why step 2 comes first.

Confirm it came up: the startup log reads `Telegram webhook registered at
https://.../webhooks/telegram`. Then message the bot.

## Running both channels at once

While `MESSAGING_CHANNEL=discord` and `TELEGRAM_BOT_TOKEN` are both set, either
channel can start a booking, and each booking's result comes back on the channel
it was requested from — including the 6:30 AM confirmation days later.

That works because each session and booking records the channel it came from.
Discord user IDs and Telegram user IDs are both bare numbers and are otherwise
indistinguishable, so the recorded channel is the only thing that says which API
to answer on. Rows written before this existed have no channel and fall back to
`MESSAGING_CHANNEL`, which is exactly how they behaved before.

## Groups

The bot works in a group chat: add it to the group and it will answer there,
with booking results posted back to the same group.

By default Telegram bots run in **privacy mode**, where they receive only
commands aimed at them, @-mentions of their username, and replies to their own
messages. Ordinary group chatter never leaves Telegram's servers.

That is already an "only act when addressed" filter, enforced server-side, at no
cost — so **leave privacy mode on**. The container is not woken and the LLM is
not called for messages that were not meant for the bot.

Turn it off (BotFather → `/setprivacy` → select the bot → **Disable**, then
remove the bot from the group and add it back, since Telegram caches this) only
when you want the bot to act on what the group was *discussing* before it was
addressed. That needs the preceding messages, which a filter cannot recover — so
it has to receive them. Pair it with a cheap triage that buffers unaddressed
messages and only calls the LLM once the bot is actually addressed; otherwise
every message in the group becomes an LLM call.

### Addressing is stripped before parsing

Whichever mode is on, a group message arrives addressed to the bot —
`@teetimebot book 9/5 at 9a`, or `/book@teetimebot 9/5 at 9a`. That addressing is
removed before the text reaches the parser, so the LLM sees `book 9/5 at 9a`.
Telegram marks it structurally in the update's `entities`, so the removal cuts
the marked ranges rather than pattern-matching the text: a mention of someone
else, or a `/command@otherbot` aimed at a different bot in the same group, is
left alone.

### A bare mention gets usage help, not silence

Tagging the bot and then typing the request as a *second* message — `@teetimebot`
followed by `Book 9/20 at 12p` — leaves the request unanswered: with privacy mode
on Telegram never delivers that second message, and with privacy mode off the app
drops it as unaddressed, since the bare tag started no conversation to continue.
(An unaddressed message *is* accepted while the sender has a live session — the
bot asked them something within the last 15 minutes — but a bare tag never
opens one.) The bot used to ignore the bare tag too, so the whole exchange
looked like it had been ignored.

A message that is only addressing is now answered with a short usage reply — what
to do differently (keep the request in the same message as the tag) and example
commands, each one shown with the tag. In a private chat, where there is no
addressing to get wrong, the same reply shows the examples untagged. The Discord
gateway answers a bare `@mention` the same way.

## Members group (who may use the bot)

Anyone in the members' Telegram group may use the bot, as well as anyone in
`TELEGRAM_ALLOWED_USER_IDS` (issue #239). Letting a member in means approving
their request to join the group. There is no secret version to add and nothing
to redeploy, and you never need their numeric ID.

Leaving the group, or being removed from it, is offboarding. The bot cancels
the person's pending bookings, deletes their stored Walden login, and tells
both them and the admin. A tee time already reserved at the club is left
alone. Someone who is also on the allowlist keeps access after leaving, so
nothing of theirs is deleted; the admin is told instead.

Membership is checked with `getChatMember` and cached for up to five minutes.
A failed check refuses the message and is not cached.

### One-time setup

Do the Telegram steps first and **read the chat ID last**. Turning on join
requests can convert the group to a supergroup, and the conversion gives it a
new ID.

1. **Create the group** in Telegram, and add the bot to it.
2. **Make the bot an admin** of the group. Telegram sends `chat_member` updates
   (joins and leaves) only to admins, and offboarding depends on them. The bot
   needs no admin rights beyond the role itself.
3. **Stop members from adding people.** Under the group's permissions, turn
   off "Add members" for regular members. Otherwise anyone in the group can
   let anyone else in.
4. **Create an invite link** with "Request admin approval" on. That link is
   what you send a new member. Approving the request is the step that gives
   them access.
5. **Read the chat ID.** From an allowlisted account, tag the bot in the group
   with any request, then read the ID out of the service's log line:

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"Telegram message received"' \
     --limit=5 --format="value(timestamp,textPayload)" --freshness=1h
   ```

   The line reads `Telegram message received from <user> in chat <id>`. The
   group's ID is the negative number, for a supergroup usually starting with
   `-100`.
6. **Add the secret version:**

   ```bash
   printf '%s' "<chat id>" | gcloud secrets versions add TELEGRAM_MEMBERS_CHAT_ID --data-file=- --project=$PROJECT_ID
   ```

   Terraform creates the secret empty the first time it applies this change,
   so this step comes after that merge.
7. **Turn it on.** Change the default of `telegram_group_access_enabled` to
   `true` in `terraform/variables.tf`, and merge. With the group configured,
   webhook registration asks Telegram for `chat_member` updates as well as
   messages.

### Checking it works

Have a second account request to join, then approve it. The bot should
welcome that account in the group with a link to a private chat, and the
account should then be able to message the bot.

Remove the account again. Both the account and the admin should get an
offboarding message.

**If no welcome appears, the bot is not receiving `chat_member` updates.**
Check that it is an admin. Without those updates, a newly approved member is
still authorized within a minute, because the membership check asks Telegram
directly. A removed member, though, keeps access for up to five minutes, and
**their scheduled bookings and stored login are not cleaned up**. Until that is
fixed, offboard by hand: cancel their bookings and run
`scripts/add_walden_credential.py remove <telegram user id>`.

## Connecting a Walden login (members do it themselves)

A member connects their own Walden login (#240). The admin never sees it.

1. In a private chat with the bot, the member sends `/start`, or taps the link
   in the group's welcome message. The bot replies with a **Connect Walden
   account** button, and pins the same form to the chat's menu button.
2. The button opens a form inside Telegram (a Mini App) at
   `<service URL>/onboarding/walden`. The member types their Walden member
   number and password and ticks a consent box.
3. The form posts straight to the service over HTTPS, with the data Telegram
   signed when it opened the form. That signature is the only thing that
   says who the member is, so nobody can save a login for someone else. The
   password is never a chat message.
4. The service checks the login with Walden over direct HTTP (#241), which
   takes about 1.5s. It saves the login only if Walden accepted it.

Commands, all answered without the language model:

| Command | What it does |
|---|---|
| `/start` | Opens the form, or says a login is already connected |
| `/login` | Opens the form, to update the login after a password change |
| `/status` | Says whether a login is connected and when Walden last accepted it. Never shows the login |
| `/forget` | Asks for `/forget confirm`, then deletes the login and cancels pending bookings |

In a group, each command answers with a link to the private chat instead.

**Limits.** Walden's lockout policy is unknown, so a member gets 3 rejected
logins an hour before being asked to wait, and 10 attempts an hour in total.

**Capacity.** When the number of connected logins reaches
`racer_max_requesters`, the admin gets a message saying to raise it. Beyond
that number, extra members race after the window opens.

**Needs:** `TELEGRAM_WEBHOOK_BASE_URL` set to an `https://` URL (terraform
fills in the service URL), and `CREDENTIAL_ENCRYPTION_KEY` mounted to encrypt
the login. Turn on group access ("Members group" above) only once this has
shipped. Until then, the welcome message's link leads nowhere useful.

**Not verified against a real Telegram client:**
- That Telegram passes the signed data to this page in the URL fragment for
  an inline button. The official script reads it from there.
- That the page closes itself after saving.
- That Telegram Web is allowed to frame it (`frame-ancestors`).

Test on iOS and Android before inviting anyone.

## Booking for someone else (admin proxy)

One designated Telegram account can book under a *specific friend's* Walden
membership — for diagnosing a failed booking, checking that a newly added
credential actually works, or just helping someone out. Set
`TELEGRAM_ADMIN_USER_ID` to that one ID (it must also be in
`TELEGRAM_ALLOWED_USER_IDS`; the allowlist is checked first, so an admin missing
from it is simply ignored). Every other user keeps booking only for themselves.

A booking runs under the login of whoever it is *for*, and that person needs a
row in `walden_credentials`. There is no shared account: a requester with no row
is told "your account isn't set up for booking yet" rather than quietly booking
under someone else's membership.

For a proxy booking that person is the friend, not the admin. `for @friend`
requires the friend to have a row and requires nothing of the admin's own account —
which has no Walden login at all, by design, and is refused one if anything
tries to look it up.

**Prerequisite: the per-friend credential store must be on**
(`credential_store_enabled = true`, with a versioned
`CREDENTIAL_ENCRYPTION_KEY`). Proxy booking always books under a friend's stored
login and never falls back to the shared account, so without the decryption key
mounted every proxy booking fails — at 6:30, days after it was accepted, since
nothing decrypts until the attempt runs. Terraform enforces this with a
precondition rather than letting it deploy.

```
admin:
@northgateteetimebot for @friend book 9/12 at 8a

NorthgateTeetimebot:
@admin I'll book a tee time for Friend for Saturday, September 12 at
08:00 AM for 4 players. Reply 'yes' to confirm.
```

Leave the target out and the bot asks for it, holding the request across the
turn:

```
admin:
@northgateteetimebot book 9/12 at 8a

NorthgateTeetimebot:
@admin For which user? This account has no Walden login of its own, so
every booking has to be made under a friend's. Reply with their name or
Telegram handle.

admin:
@friend
```

What "@friend" matches is the `--name` and `--telegram-username` fields on that
friend's credential row:

```bash
poetry run python scripts/add_walden_credential.py set <telegram user id> \
    --name "Friend" --telegram-username <friend-username>
```

Matching is case-insensitive. The leading `@` is **required** in the `for @X`
clause — it is what separates a target from ordinary English, so that
`for 4 players, book 9/12` is read as a booking rather than as a friend named
"4". It is only a marker, not a claim that the word is a Telegram handle: a
stored `--name` matches through it fine. Omitting it costs a turn rather than
the request (`for friend book 9/12` is parsed as a booking, then the bot asks who
it is for), and answering that question takes a bare name either way.

`--label` is *not* matched — it stayed a free-text admin note. `list` shows
which rows have neither and are therefore not addressable.

Three properties are deliberate, and all three exist to stop a round being
booked under the wrong membership:

- **The admin has no Walden login of its own.** No row in `walden_credentials`,
  and it does not fall back to the shared global account either — a booking
  attributed to the admin is refused outright, in the conversation and again in
  the credential lookup.
- **An unresolved or ambiguous target fails loudly.** A typo, a friend who has
  not been added yet, or a name colliding with someone else's handle all come
  back as a question, never as a guess.
- **The booking is the friend's, the conversation is the admin's.** The record
  carries the friend's identity, so it runs under their membership and appears
  in their history, and the confirmation or failure days later goes to *their*
  conversation. The admin gets only the immediate acknowledgement, in their own
  chat.

Cancelling or checking status on someone else's behalf is not supported yet;
the bot says so rather than applying it to the admin's own history.

## How it maps onto the existing design

- `app/providers/telegram_provider.py` implements the same `SMSProvider`
  interface Twilio and Discord use; outbound "SMS" become Bot API `sendMessage`
  calls.
- `POST /webhooks/telegram` in `app/api/webhooks.py` replaces the Discord
  gateway's `on_message`. It verifies the shared secret, checks the allowlist,
  and routes into `booking_service.handle_incoming_message()` with the chat the
  message arrived in.
- The `phone_number` identifier field carries the Telegram user ID (numeric,
  fits the existing 20-char column); `origin_channel_id` carries the chat ID,
  which is negative for groups.
- An update that is merely uninteresting — another user, no text, an update type
  we did not ask for — is answered with 200. Telegram retries any non-2xx
  response, and retrying will not make an ignored message interesting.
