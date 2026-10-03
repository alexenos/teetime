---
layout: page
title: How to join
permalink: /join/
---

# How to join the tee time bot

The bot books Northgate tee times for you the moment reservations open, 7 days
ahead at 6:30am. It books under **your own** Walden membership, so you connect
your Walden login once. You do that yourself, and nobody else sees it, including
the person who invited you.

You need about ten minutes, your phone, and your Walden member number and
password.

## 1. Get Telegram

Install **Telegram** from the App Store or Google Play, open it, and sign up with
your phone number.

To keep your phone number private from other group members, go to
**Settings → Privacy and Security → Phone Number** and set **Who can see my phone
number** to **Nobody**.

## 2. Join the group

Tap the invite link you were sent, then **Request to Join**. You'll be let in
once your request is approved, usually within the day.

When you're in, the bot posts a welcome message in the group with a link.

## 3. Connect your Walden account

1. Tap the link in the welcome message. It opens a private chat with the bot.
   Tap **Start**.
2. The bot replies with a **Connect Walden account** button. Tap it.
3. A form opens inside Telegram. Read it, enter your **Walden member number** and
   **password**, tick the box, and tap **Connect**. Your phone's password manager
   can fill these in.
4. The bot checks your login with Walden, which takes a few seconds:
   - **"Connected"**: you're done.
   - **"Walden didn't accept that login"**: check it on the Walden website, then
     try again. After three wrong tries the form asks you to wait an hour, so
     your Walden account isn't locked.
   - **"Couldn't get an answer from Walden"**: nothing was saved. Try again in a
     few minutes.

**Never send your password as a chat message**, to the bot or to anyone. The form
is the only place it goes.

## 4. Book a tee time

In your private chat with the bot, just say what you want:

- `Book Saturday 8am for 4 players`
- `Book 9/20 at 12p for 2`

The bot confirms the details. If that exact time isn't on the tee sheet, it
offers the nearest real ones. At 6:30am on the day reservations open, it races
for your time and tells you what it got.

You can also ask in the group. Start your message by tagging the bot.

## Managing your login

Send these to the bot in your private chat:

| Command | What it does |
|---|---|
| `/status` | Says whether your login is connected and when Walden last accepted it. It never shows the login itself. |
| `/login` | Opens the form again. Use it after you change your Walden password. |
| `/forget` | Deletes your saved login and cancels bookings the bot hasn't made yet. It asks you to confirm first. |

If you leave the group, the bot does the same as `/forget` automatically.
Tee times already reserved at the club are yours either way. Cancel those with
the club if you need to.

## Questions

Ask the person who invited you. How your information is handled is in the
[Privacy Policy](/teetime/privacy/), and the rules are in the
[Terms](/teetime/terms/).
