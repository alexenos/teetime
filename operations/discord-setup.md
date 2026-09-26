# Discord Bot Setup

TeeTime can use Discord DMs instead of SMS/WhatsApp as the conversation
channel (`MESSAGING_CHANNEL=discord`). Inbound messages arrive over the
Discord gateway (a persistent WebSocket), so the service must run with
`min-instances=1` on Cloud Run — it cannot scale to zero and still hear you.

## One-time setup (Discord side)

1. **Create a private server** (skip if you have one): in the Discord app,
   click **+** in the server list → *Create My Own* → *For me and my friends*.
   The bot DMs you directly, but Discord only delivers bot DMs when you share
   a server with the bot, so this server is just the meeting point.

2. **Create the application**: go to
   <https://discord.com/developers/applications> → **New Application** → name
   it `TeeTime` → Create.

3. **Get the bot token**: left sidebar → **Bot** → **Reset Token** → copy the
   token. Treat it like a password: put it straight into `.env`
   (`DISCORD_BOT_TOKEN=...`) or Secret Manager — don't paste it into chats or
   commit it.

4. **Enable the Message Content intent**: still on the **Bot** page, under
   *Privileged Gateway Intents*, toggle **Message Content Intent** ON and
   save. Without this the bot receives empty message bodies.

5. **Invite the bot to your server**: left sidebar → **OAuth2** →
   **URL Generator** → check the `bot` scope → under *Bot Permissions* check
   **Send Messages** and **View Channels** → open the generated URL in your
   browser → pick your server → Authorize.

6. **Get your user ID**: in Discord, *User Settings → Advanced → Developer
   Mode* ON, then right-click your own name in any chat → **Copy User ID**.
   This goes in `DISCORD_USER_ID` and is the allowlist — the bot ignores
   everyone else.

7. **(Optional) Get a fallback channel ID**: with Developer Mode on, right-click
   a channel → **Copy Channel ID** and put it in `DISCORD_CHANNEL_ID`. Replies
   already follow the conversation on their own (see below), so this is only the
   destination for notifications with no conversation behind them — bookings
   made through the REST API, and any created before this behavior existed.
   Make sure the bot has **Send Messages** and **View Channels** permission
   wherever you talk to it.

## App configuration

```dotenv
MESSAGING_CHANNEL=discord
DISCORD_BOT_TOKEN=<token from step 3>
DISCORD_USER_ID=<id from step 6>
DISCORD_CHANNEL_ID=<id from step 7, optional>
```

Locally: put these in `.env` and run the server; the gateway starts inside
the FastAPI process (log line: `Discord gateway connected as TeeTime#...`).
Message the bot — message flow is identical to the old SMS flow. Talk to it in
your shared channel by @-mentioning it, or send a DM.

### Where replies go

Replies land wherever the conversation started. Ask for a booking in
`#general` and the result — including the 6:30 AM confirmation or failure that
arrives days later — is posted in `#general`, mentioning you. Ask in a DM and
everything stays in the DM.

This works because the channel of each incoming message is recorded on your
session and copied onto any booking it creates, so the notification can find
its way back long after the request. Bookings with no channel recorded (the
REST API, SMS, or bookings made before this existed) fall back to
`DISCORD_CHANNEL_ID`, and to a DM if that is unset.

For Cloud Run, add the two secrets and set
`--min-instances=1` so the gateway stays connected.

## How it maps onto the old SMS design

- `app/providers/discord_provider.py` implements the same `SMSProvider`
  interface Twilio used; outbound "SMS" become channel posts or DMs via the
  Discord REST API.
- `app/services/discord_gateway.py` replaces the Twilio inbound webhook;
  messages from `DISCORD_USER_ID` are routed into
  `booking_service.handle_incoming_message()` along with the channel they
  arrived in.
- The `phone_number` identifier field now carries your Discord user ID
  (a numeric snowflake, fits the existing schema). Twilio remains available
  by setting `MESSAGING_CHANNEL=twilio`.
