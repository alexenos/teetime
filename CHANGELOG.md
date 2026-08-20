# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
