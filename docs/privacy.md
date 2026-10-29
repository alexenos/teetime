---
layout: page
title: Privacy Policy
permalink: /privacy/
---

# Privacy Policy

**Last updated: October 25, 2026**

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

The code that handles your login is public:
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

- **Your Walden login:** until you delete it or leave the group. If Walden
  rejects it and you do not save a new one, it is deleted after 90 days.
- **Booking records:** kept, with no automatic deletion. They let the bot tell
  you what it booked, let problems be diagnosed, and let the group's booking
  patterns be studied over the years. After you send `/forget` they are kept
  under your label instead of your Telegram account (see below).
- **Conversation state:** deleted after 90 days without a message from you.
- **Your label** (such as "Member C"): kept, so an old public race report never
  comes to mean someone else. After `/forget`, nothing links it back to you.
- **Service logs:** 30 days.
- **Copies of the club's tee-sheet pages** taken during bookings, which can show
  members' names: about 90 days, then up to two more weeks in a recovery
  window before they are gone for good.
- **Backups** of the database are kept for a short rolling period, so a row
  deleted from the database can remain in a backup for up to about a week.

## Deleting your information

**`/forget`**, sent in your private chat with the bot:

- deletes your saved Walden login;
- cancels bookings it has not made yet;
- deletes the state of your conversation with the bot;
- takes your identity off your booking history. Your past bookings are kept,
  for the group's statistics, but under your label instead of your Telegram ID:
  your @username, your private chat with the bot, and any error text are
  removed from them. A booking still to be played keeps your ID until it is
  played, because the bot needs it to tell you how it went, and is changed
  within a day after that.
- Once none of your bookings is left to change, the link between your Telegram
  account and your label is deleted too. The label is not given to anyone else.
  If you join again later, you start as a new member with a new label.

This history is **pseudonymised, not anonymous**: it no longer names you, but
someone who knew your booking habits well could still recognise them. If the
person who runs the bot has listed you by hand under your label, they are told
to remove that entry. Database backups can hold the earlier version for up to
about a week.

**Leaving the group** deletes your saved Walden login and cancels bookings the
bot has not made yet. It does **not** take your identity off your booking
history: send `/forget` before or after leaving if you want that too.

For anything else, ask the person who invited you.

Tee times already reserved at the club are your own, and are not affected.

## Changes

If this policy changes, the date at the top changes with it.

---

*TeeTime Bot is a private application for invited members only.*
