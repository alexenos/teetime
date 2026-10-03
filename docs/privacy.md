---
layout: page
title: Privacy Policy
permalink: /privacy/
---

# Privacy Policy

**Last updated: October 3, 2026**

TeeTime Bot is a private, invitation-only Telegram bot that books golf tee times
at Northgate Country Club for members of a small group, each under their own
Walden Golf membership. This page says what it stores, where, who can see it,
and how to delete it.

## What is stored

**About you, from Telegram**
- Your numeric Telegram user ID.
- Your first name and Telegram @username, if you have one.

These identify your requests, let the bot reply to you, and let the group's
admin ask the bot to book on your behalf.

**Your Walden login**
- Your Walden member number and password, which you enter yourself in the bot's
  setup form. The bot uses them only to log in to Walden as you and book the
  tee times you ask for.
- When Walden last accepted the login.

**Your bookings and conversations**
- Your booking requests: date, time, number of players. Also the result, and
  any confirmation the club returned.
- The state of your current conversation with the bot, for example a booking
  waiting for you to confirm.

**A label**
- A pseudonym such as "Member C", used instead of your name or ID in the
  service's logs and in the public race reports described below.

## How your Walden login is protected

- **It never passes through chat.** The setup form sends it from your phone
  directly to the booking service over an encrypted connection. It is never a
  Telegram message, so it is not in your chat history or on Telegram's servers.
- **It is encrypted at rest with a Google Cloud KMS key** that nobody can copy
  or export.
- **Every decryption is recorded** in an audit log, along with which account
  made it.
- **It is checked with Walden before it is saved,** and it is never shown back
  to you or anyone else.

**What this cannot promise.** The booking service has to decrypt your login to
use it, and the person who operates the service controls the Google Cloud
project it runs in. That person could, deliberately, read a login, and doing so
would leave a record in the audit log. During a short transition period while
the service moves to the KMS key, each login also keeps a backup copy encrypted
with an older key held by the operator, which is not audit-logged. That copy is
deleted when the transition ends. The code that handles your login is public:
[github.com/alexenos/teetime](https://github.com/alexenos/teetime).

## Who your information goes to

- **Telegram** carries your messages to and from the bot. See
  [Telegram's privacy policy](https://telegram.org/privacy).
- **Google Cloud** (United States) runs the bot and stores everything above.
- **Google's Gemini AI** receives the text of your booking messages, for example
  "Book Saturday 8am for 4", to understand what you are asking for. It does not
  receive your Walden login.
- **Walden Golf**, the club's booking site, receives your login when the bot
  books for you, exactly as if you had logged in yourself.

Nothing is sold or shared for marketing.

## Public race reports

The project publishes reports about how each morning's booking race went, in a
public code repository. They refer to people only by label ("Member A",
"Rival 1"), never by name, Telegram handle, ID or member number. Every report is
checked automatically for names before it is published.

## How long things are kept

- **Your Walden login:** until you delete it or leave the group.
- **Booking records:** kept, so the bot can tell you what it booked and so
  problems can be diagnosed.
- **Service logs:** 30 days.
- **Copies of the club's tee-sheet pages** taken during bookings, which can show
  members' names: kept for diagnosing booking problems, with no automatic
  deletion at present.

## Deleting your information

- **`/forget`** in your private chat with the bot deletes your saved Walden login
  and cancels bookings it has not made yet.
- **Leaving the group** does the same automatically.
- For anything else, such as booking records or your label, ask the person who
  invited you.

Tee times already reserved at the club are your own, and are not affected.

## Changes

If this policy changes, the date at the top changes with it.

---

*TeeTime Bot is a private application for invited members only.*
