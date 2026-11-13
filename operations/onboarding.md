# Onboarding and offboarding members

The maintainer's runbook for self-service onboarding (#179, #239–#244, #256).

The maintainer's step per member is to approve a join request. The member does
the rest, and the maintainer never sees their Walden login.

The member-facing guide is public at `alexenos.github.io/teetime/join/`
(`docs/join.md`). This file is the maintainer's side.

## Before inviting anyone new

All of these must be true. As of 2026-10-03 the last two are not.

| | Where it stands |
|---|---|
| Group access on (`telegram_group_access_enabled`) | #258 |
| Setup form live (`/start`, `/login`, `/status`, `/forget`) | #253, merged and deployed |
| KMS key created | #255, applied 2026-10-03 18:57Z |
| KMS turned on (`credential_kms_enabled`) | #260 |
| Pseudonyms assigned at onboarding | #257 |
| **A real member has connected a login and raced with it** | Member B's test, after #260 |
| **The Fernet key is retired**, so no member's login has a copy the maintainer could read | Steps 5–6 of `operations/credential-encryption.md`, then the retirement PR |

Until the Fernet key is retired, every login saved also keeps a Fernet fallback
copy, which is readable with the hand-made key and not audit-logged. Inviting
someone before then breaks the promise in `docs/privacy.md` for them.

## One-time group setup

Done on 2026-10-03, except where noted. The details are under "Members group" in
`operations/telegram-setup.md`.

- The bot is an **admin** of the members group, with no admin rights beyond the
  role. Telegram sends join and leave events only to admins.
- **Edit → Permissions → Add Users: off** for members. *(Maintainer to confirm.)*
- **Edit → Invite Links → Create a New Link, with Request Admin Approval on.**
  Share only this link. Revoke the primary link if it was ever shared.
  *(Maintainer to confirm.)*
- The `TELEGRAM_MEMBERS_CHAT_ID` secret holds the group's ID: one version,
  added 2026-10-03.

## Per member

1. **Send them two things,** privately, never in the repository: the
   approval-required invite link, and the guide at
   `https://alexenos.github.io/teetime/join/`. A message that works:

   > The tee time bot books Northgate times for you at 6:30am, 7 days out. Join
   > here: <invite link>. Then follow https://alexenos.github.io/teetime/join/.
   > It takes ten minutes, and I never see your Walden password.

2. **Approve their join request** when Telegram notifies you. The bot then
   welcomes them in the group with a link to a private chat.
3. **They connect their login** with the form. You get two messages:
   - "… is Member X in logs and race reports". **Add them** to
     `MEMBER_PSEUDONYM_REGISTRY` and `MEMBER_PSEUDONYM_LABELS` as that label,
     with their name forms and Telegram handle, so the race-report name check
     knows them.
   - If stored logins have reached `racer_max_requesters`, a warning saying so.
     Raise it in `terraform/variables.tf` (default 4, maximum 10).
4. **Check it took**, if you like: `scripts/add_walden_credential.py list` shows
   their row with `pseudonym=`, `encryption=kms` and `fallback=`. It never shows
   the login.

Their first race is the real test. `CREDENTIAL_FALLBACK` in that morning's log
means KMS failed and the fallback copy was used: fix KMS before inviting anyone
else.

## Offboarding

**Remove them from the group.** The bot then:
- cancels their bookings that have not been made yet;
- deletes their stored login;
- tells them and you.

Tee times already reserved at the club are left alone. Their pseudonym is kept,
so it is never reused.

**Exception:** someone also on `TELEGRAM_ALLOWED_USER_IDS` keeps access after
leaving, and nothing of theirs is deleted. You get a message saying so instead.

**If no offboarding message arrives,** the bot is not receiving `chat_member`
updates. Check that it is still an admin. Until that is fixed, offboard by hand:
cancel their bookings, and run
`scripts/add_walden_credential.py remove <telegram user id>`.

## Who has read a login

Every KMS decrypt is audit-logged with the account that made it. The query is
under "Who has read a login" in `operations/credential-encryption.md`. Only
`teetime-run` and `teetime-observer` should appear. Any person's address is
someone reading a member's password.

## When things go wrong

| Symptom | Likely cause | What to do |
|---|---|---|
| No welcome after approving someone | The bot is not getting `chat_member` updates | Check the bot is an admin. Members are still authorized within a minute, because the bot asks Telegram directly |
| A member says the form shows "open from the bot's button" | The form was opened outside Telegram, or more than 10 minutes ago | Close it, and tap the button in the bot chat again |
| "Walden didn't accept that login" for a correct password | The page Walden returns has changed | `scripts/probe_http_login.py compare` with a known login. See #241 |
| "Couldn't get an answer from Walden" for everyone | Walden is down, or blocking the service's requests | Nothing is saved, so members can retry later. Check from the service's egress |
| A race fell back to the Fernet copy (`CREDENTIAL_FALLBACK`) | KMS was unreachable, or a permission is missing | Read the error in the line. Check the key's IAM in `terraform/kms.tf` |
| A booking failed with "Could not resolve booking credentials" | No login stored, or neither KMS nor the fallback could decrypt it | `list` shows whether a row exists. Ask the member to `/login` again |
