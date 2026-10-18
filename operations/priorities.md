# Priorities

What matters most, in order, and what to leave alone. The feature-dev Routine
(`operations/routines/feature-dev.md`) ranks the backlog by this file and builds the
best eligible issue. **The maintainer owns it.** Change the order, rewrite a theme or
add a hold, and the next run follows it. Nothing else needs touching: no labels.

**DRAFT.** A session wrote the themes below on 2026-10-18 from issue titles and project
notes, not from the maintainer. Rewrite them. Until then they are a guess, and the
Routine treats them as the maintainer's word.

## What matters, in order

Write themes as a sentence or two a stranger could match an issue against. The Routine
quotes the part of an issue that made it match, so vague themes produce vague matches.

1. **Winning the 6:30 race, and never losing a booking silently.** Anything that
   changes whether a member gets the tee time they asked for, or that leaves a booking
   stuck or unreported if the service crashes mid-run.
2. **Members can use the bot without the maintainer.** Joining, connecting a login,
   leaving, having their data removed, and not needing anyone's help to book.
3. **Members always know what happened.** Clear confirmations and failure messages,
   including why a time could not be booked.
4. **Cheap and boring to run.** Cost, infrastructure checks and anything that makes
   the deployment safer to change.
5. **Anything else a member asked for.**

Cleanup (lint suppressions, dead code, stale docs) is the tech-debt Routine's, not
this one's.

## Hold

Issues or themes the Routine must not build, whatever their rank. Leave empty if none.
An issue stays ranked and shown; it is only removed from the build set. Closing a
Routine PR unmerged also stops that issue being rebuilt.

- *(none yet)*
