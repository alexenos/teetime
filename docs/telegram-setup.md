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

4. **Invent a webhook secret**: any random string, e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Telegram does
   not sign webhook payloads, so this shared secret is the only thing standing
   between a public endpoint and a forged booking. Without it the endpoint
   rejects every update.

5. **Start a chat**: open your bot and send `/start`. A bot cannot message you
   first, so until you do this, outbound notifications have nowhere to go.

## App configuration

```dotenv
TELEGRAM_BOT_TOKEN=<token from step 2>
TELEGRAM_ALLOWED_USER_IDS=<id from step 3>
TELEGRAM_WEBHOOK_SECRET=<secret from step 4>
TELEGRAM_WEBHOOK_BASE_URL=https://<your Cloud Run URL>
```

`TELEGRAM_WEBHOOK_BASE_URL` is what the service registers with Telegram at
startup. Leave it unset locally: there is no public URL to register, outbound
still works, and inbound simply does not run.

Registration is idempotent and re-run on every startup, so a changed service URL
heals itself rather than leaving the bot pointed at a dead endpoint.

## Deploying it

Terraform creates the three secrets **empty**. A Cloud Run revision that
references a secret with no version fails to deploy, so create the versions
first, then enable the channel:

```bash
PROJECT_ID="teetime"
printf '%s' "<bot token>"     | gcloud secrets versions add TELEGRAM_BOT_TOKEN --data-file=- --project=$PROJECT_ID
printf '%s' "<your user id>"  | gcloud secrets versions add TELEGRAM_ALLOWED_USER_IDS --data-file=- --project=$PROJECT_ID
printf '%s' "<webhook secret>"| gcloud secrets versions add TELEGRAM_WEBHOOK_SECRET --data-file=- --project=$PROJECT_ID
```

Then turn the channel on by changing the **default** of `telegram_enabled` to
`true` in `terraform/variables.tf`, and deploy. It has to be the default rather
than a `terraform.tfvars` entry: `*.tfvars` is gitignored, so it is absent from
the Cloud Build checkout, and `cloudbuild.yaml` passes only `project_id`,
`region`, `container_image` and `log_level`. Every other variable resolves to
its default. `TELEGRAM_WEBHOOK_BASE_URL` is filled in from the service's own URL
automatically.

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

By default Telegram bots run in **privacy mode**, where they only receive
messages that are commands or direct replies to the bot. To have the bot see
ordinary group conversation — the prerequisite for booking from what the group
is already discussing — message BotFather → `/setprivacy` → select the bot →
**Disable**. Telegram caches this, so **remove the bot from the group and add it
back** for the change to take effect.

Leave privacy mode **on** unless you actually need ambient messages: with it off,
every message in the group reaches the service, and each one is a wake-up for a
scale-to-zero container.

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
