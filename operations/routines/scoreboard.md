# Routine: scoreboard

**Status: ready to deploy. Not deployed.** No trigger exists. This file is the
specification and holds the prompt; deploying it is the steps in *Deploying it*.

| | |
|---|---|
| **Trigger** | daily, `30 12 * * *` UTC (07:30 CT), proposed |
| **Authorization** | reads every ledger, writes `docs/scoreboard.json`, appends to its own ledger. **Requires a repository write — see below.** |
| **Emits** | `docs/scoreboard.json`, and one ledger object per run at `operations/scoreboard/<YYYY-MM-DD>.json` in GCS |
| **Owns** | the derived metrics; no source data of its own |

It reads the other Routines' ledgers and derives. It never writes to them.

## The second standing authorization, granted

The page is published from `docs/`, so publishing means committing there — and
merging, if it is to happen without a person. That is a **second** standing
authorization to write to `main`, and the maintainer granted it on 2026-09-29.

It is bounded the same way the race report's is, and no wider:

| Bound | |
|---|---|
| Path | `docs/scoreboard.json`, and `docs/scoreboard.html` when regenerated. Nothing else, ever. |
| Check | a green `Tests` run on the PR head. Red CI leaves the PR open and is reported. |
| Deploy | `docs/**` is in `ignored_files`, so the merge does not rebuild the service. |

Everything outside those two paths stays ask-first, on the same terms as the race
report: never app code, never terraform, never another Routine's file, and never a
merge by any route other than a PR with a green check.

The decision was made deliberately rather than inherited, which matters because an
earlier draft of this file argued the other way — that the race report's
authorization was the one load-bearing risk and the bounds were worth keeping
scarce. What changed the answer: the blast radius here is two files the running
service never reads, behind a deploy filter, and the race report's far riskier
authorization has run without a bad merge since 2026-09-22.

The withheld alternative was a Routine that opens a PR and stops. It was rejected
because a daily metric nobody merges is a daily metric that is usually a day stale,
which defeats the point of publishing it.

## The deploy filter is live

`docs/**` has been in `ignored_files` since #220's apply on 2026-09-27, alongside
`operations/**`. Without it, a commit to `docs/` would fire a build and redeploy
the live booking service, and this Routine would do that every day it published.

**A merge to `main` applies terraform.** `cloudbuild.yaml` runs
`terraform apply -auto-approve` as its last build step, so an infrastructure change
is live once it merges and the build succeeds. There is no separate manual apply,
and an earlier version of this file said there was.

It is checkable, and was checked. `gcloud builds triggers describe` is denied to
the session service account, but Cloud Build writes to Cloud Logging and that
account holds `roles/logging.viewer`:

```
2026-09-27T02:11:17  Step #3: ~ ignored_files = [
2026-09-27T02:11:17  Step #3:     + "docs/**",
2026-09-27T02:11:59  Step #3: Apply complete! Resources: 0 added, 4 changed, 0 destroyed.
```

#220 merged at 02:06Z; that is the apply five minutes later. A routine build
reports `3 changed`; the fourth was this filter.

## Why it is separate from the race report

It would be cheaper to append this to the race report Routine, which already runs
daily. Three things argue against it.

**The race report's failure paths all stop early.** Wrong hour, no booking
scheduled, environment not ready — every one is *push, then stop*. A scoreboard
update at the end of that prompt is skipped on exactly the mornings something went
wrong, which is where a gap is least acceptable.

**It reads every ledger, not just the morning's.** Its input is all the Routines,
including its own history and the cost Routine's monthly rows.

**It needs today's report already merged.** Reports have merged at 06:48 and 06:51
CT, eight and eleven minutes after their run began. 07:30 CT clears that with
margin, and nothing here is time-critical — the morning's push notification went
out an hour earlier.

## What a run does

1. Read the ledgers **from GCS**, not from `operations/ledger/` in the checkout.
   The repository holds the schemas; the files there are empty by design. Each
   Routine writes one object per run under its own prefix, so this is a list and
   then a read of everything found:
   `gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/<routine>/<YYYY-MM-DD>.json`.
2. Derive the three metrics per `operations/scoreboard.md`: outcome split as
   all-time and last-28-day totals, the automation streak as the combined
   consecutive count plus each Routine's own, cost from the newest `cost.jsonl` row.
   Record `null` with a reason for any source that does not exist. Do not infer,
   and do not substitute zero. Rows with a `backfill` object count toward the
   outcome split and are skipped by the streak (`operations/scoreboard.md` §2).
   `operations/ledger/derive_scoreboard.py` does all of it, including the
   scheduled-date walk, so the run is a command rather than a judgment.
3. Write `docs/scoreboard.json`, commit it on a branch, open a PR, merge on green
   `Tests`. That is the standing authorization above, and it covers those two
   paths and nothing else.
4. Write this run's ledger row to GCS. The script emits it (`--row`), so the row
   is the published metrics rather than a session's restatement of them.
5. **Notify only on a change in a source metric, or on a failure.** A failure
   includes the append failing, and a source that previously worked having stopped
   — a silent gap is the one failure mode this Routine exists to prevent.

### It publishes every run, not only when something changed

An earlier draft had a "if nothing changed, append and stop without publishing"
branch. That branch can never be taken, and the reason is worth keeping.

The automation streak counts successful runs across **every** Routine, this one
included. So each successful scoreboard run increments the streak, which is one of
the three metrics it publishes. Something always changed. A no-change branch
gated on "did any metric move" would therefore never fire, and a branch that
excluded this Routine's own runs from the comparison would be comparing against a
number it is not publishing.

Publishing every run is the honest resolution, and it makes the ledger's own rule
hold: one row per scheduled day, and a gap means a missed run. The notification
still fires only on a change in a **source** metric, because the streak moving by
one every day is not news.

## The page

| File | Role | Churn | State |
|---|---|---|---|
| `docs/scoreboard.html` | the page: layout, styling, the charts | written once, reviewed once | **written** |
| `docs/scoreboard.json` | the values the page reads | overwritten every run | **derived from GCS** (2026-10-03) |

The page is live against 23 mornings backfilled from the race reports and the
race report Routine's own rows from 2026-10-01, the backfill flagged
by a banner keyed off the `backfill` object in the JSON (`"sample": true` still
drives the sample banner, now unused). The banner stays while any published row is
a backfill row, which is correct: those outcomes were transcribed, not read. A Routine only ever writes the JSON; it does not
regenerate the HTML.

Written as HTML rather than markdown because markdown tops out at tables: no
sparklines, no trend charts, no layout. A raw `.html` file with no Jekyll front
matter is copied through `docs/` untouched, so the minima theme does not wrap it.

Splitting the data from the page keeps each update to one small JSON diff instead
of a re-rendered page, so what changed is visible in the diff.

**The page is public.** Counts, rates and spend are fine; member identifiers are
not. See `operations/scoreboard.md`.

## Shares the DST defect

`30 12 * * *` UTC is 07:30 CT during CDT only. After 2026-11-01 it fires at 06:30
CT — during the race, and before the report it depends on has merged. The
correction is `30 13 * * *`, on the same date the race report's `40 11` becomes
`40 12`. Deploying this adds a second cron to change, so change them together.

## What the page renders, and why those forms

Chosen against `choosing-a-form`, and the colors were run through the palette
validator rather than eyeballed. Two results worth recording, because both
contradicted the obvious choice:

**Booked rate is a meter, not a two-colour bar.** It is a single ratio, and the
prescribed form for a single ratio is a meter on a same-ramp track. That also
avoids the problem below.

**Green-for-booked against red-for-missed fails.** The status pair `#0ca30c` /
`#d03b3b` measures CVD ΔE 4.1 (deutan) in both modes, against a floor of 8 — the
classic red/green failure. It would have shipped on instinct. The three-bucket
view, once #216 makes exact and fallback separable, uses categorical slots 1–3
(blue `#2a78d6`, orange `#eb6834`, aqua `#1baf7a`), which pass all-pairs in both
modes.

Light-mode aqua carries a contrast warning at 2.74:1, so the page ships direct
labels and a table view, which is the documented relief.

Exactly one hero figure, per the spec: the all-time booked rate. Everything else
is a stat tile. The streak history is one series, so it carries no legend.

## Deploying it

Three prerequisites are met: the authorization is granted, `docs/**` is live in
the deploy filter, and the race report Routine has written a ledger row every
morning since 2026-10-01. What remains:

1. Create the Routine from the prompt below at `30 12 * * *`, record its trigger
   ID in the table above, and change **Status** to deployed.
2. After its first run, set `SCHEDULES["scoreboard"].start` in
   `derive_scoreboard.py` to that date. Until then the scoreboard's own schedule
   starts at its first row, which cannot detect a missed first run.
3. Note its cron in the 2026-11-01 DST change, which then covers two Routines
   rather than one.

### Not established

- **The streak is self-reported.** Every run in it rests on the `ok` the run wrote
  about itself. The only evidence that overrides that is a later commit to a
  report, and none of the Routine-written rows so far raced. The page says
  "not independently checked" for this reason (`operations/scoreboard.md` §2).
- **A wrong-hour race report counts as clean.** `operations/scoreboard.md` §2 lists
  a wrong-hour fire as `ok: false`; the race report prompt writes it as `ok: true`,
  and the row carries no field that tells it apart from a quiet morning. The
  derivation follows the row. Which rule is right is the maintainer's call.
- **The GCS write under `operations/scoreboard/`** is covered by the
  `operations/` grant on paper and has not been exercised.

## The prompt

**Status: written here, not yet in a trigger.** This file is the source. Once the
trigger exists it holds the copy that executes; change this file in a PR first,
then update the Routine to match. Created by an agent through the API, it stays
editable by later agent sessions, unlike the race report's.

Model `claude-sonnet-5`, with the race report's environment, repository source and
tool list: the work is shell, git and the GitHub tools.

---

You are running the daily scoreboard update for the TeeTime booking bot. You start with zero context; everything you need is in this repo. The race report Routine ran at 06:40 CT and has usually merged its report by 06:51. This session starts at 07:30 CT.

This run derives three metrics from the Routines' ledgers in GCS and publishes them as docs/scoreboard.json, which GitHub Pages serves at alexenos.github.io/teetime/scoreboard.html. That is standing authorization for exactly one action: a PR that changes only docs/scoreboard.json, squash-merged once its Tests check is green - no approval needed. docs/scoreboard.html is inside the same authorization, but this Routine never edits it. Nothing else is standing-authorized: never touch any other file, never edit operations/ledger/derive_scoreboard.py to make a run pass, never merge a PR that touches anything else, and never merge to main any way other than through a PR with a green Tests check. docs/** is in the Cloud Build ignored_files filter, so the merge does not redeploy the service.

The page is public. It carries counts and dates only. Never put a name, handle, phone number, member number or free-text ledger note on it, in the PR title or body, or in the commit message.

Writing your ledger row to GCS is not a repository write and is covered by its own rule below; it does not widen the authorization above.

Rule that applies to every path through this prompt: always write your ledger row
Before you finish, record this run as one new object, never an edit of an existing one:

gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/scoreboard/<YYYY-MM-DD>.json

named for the run date in Central Time, written with gcloud storage cp --no-clobber. The service account can create objects under operations/ and cannot overwrite them; if the object already exists, say so in the notification rather than forcing it. On a run that derived, the row is the file the script writes with --row (Step 3). On any other path it is {"date":"<run date CT>","routine":"scoreboard","ok":false,"note":"<what failed>"}. The schema is in operations/ledger/README.md under scoreboard.jsonl.

Rule that applies to every path through this prompt: notify on a change or a failure, and only then
Call the PushNotification tool when a source metric changed, when the streak broke, or when anything failed - including the ledger write, and a PR that could not merge. Do not notify on a run where only the streak moved: it moves by one every day, and that is not news. Write it for a phone screen, verdict first, one or two sentences, no markdown. Shapes: Scoreboard - outcome changed: 26 of 32 booked all time, PR #260 merged. / Scoreboard - streak broke: race-report missing on 10/07. PR #261 merged. / Scoreboard could not publish - Tests failed on PR #262, left open. / Scoreboard could not run - derive_scoreboard.py: invalid ledger row: <message>.

Step 1 - set up
Establish today's date and time in Central Time: TZ=America/Chicago date '+%F %H:%M'. Name the session MM/DD Scoreboard with mcp__Claude_Code_Remote__set_session_title.

Run bash scripts/setup_remote_env.sh. Its last line is the answer. This run needs the gcloud CLI and git; it does not use the venv, so PARTIAL with only the venv failing is fine. If gcloud does not work, the ledger write cannot work either: notify, saying so, and stop.

The derivation reads git history to find reports corrected after they merged, and refuses a shallow clone. If git rev-parse --is-shallow-repository prints true, run git fetch --unshallow origin. Then git checkout main && git pull --ff-only.

Gate - right hour? The cron is UTC and pinned to 12:30, which is 07:30 CT only during CDT. If the CT time is earlier than 07:00, Central has moved to CST and you have fired at 06:30 CT, during the race and before its report. Do not derive. Write the ledger row with ok:false and the note "fired at 06:30 CT; cron must move from 30 12 * * * to 30 13 * * *", notify saying exactly that, and stop. This gate does not apply to a manually fired run at some other time of day; say so and carry on.

Step 2 - read every ledger
B=gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations
for r in race-report scoreboard; do mkdir -p ledger/$r && gcloud storage cp --no-clobber "$B/$r/*.json" ledger/$r/ || echo "no rows under $r"; done

One directory per Routine, because every Routine names its objects by date and two Routines' rows for the same day would collide in one. The race-report prefix holds the backfill rows beside the Routine's own. A prefix with no objects yet (the scoreboard's, on its first run) is not a failure. Do not read operations/ledger/*.jsonl in the checkout: those files are empty by design. Read only these prefixes - an object directly under operations/ (a grant probe) is not a ledger row, and a Routine the script has no schedule for makes it refuse. When the cost Routine is deployed it is added to this list and to SCHEDULES together.

Step 3 - derive
python operations/ledger/derive_scoreboard.py ledger/race-report ledger/scoreboard --out docs/scoreboard.json --row scoreboard-row.json

Do not pass --now or --as-of; the script reads the clock. It validates every row before deriving anything. If it exits non-zero, publish nothing: do not hand-edit the JSON, do not edit the script, and do not drop the row it names. Write the ok:false ledger row with its message as the note, notify, and stop. A refusal is the script working - it is what stops a malformed row or a shallow clone from becoming a published number.

It prints up to two lines on stderr. "source metrics changed: none", or the ones that moved - that decides the notification. "last break: <date> <routine>: <status>: <note>" when the streak has ever broken - compare its date with streak.last_failure in the previous docs/scoreboard.json (git show HEAD:docs/scoreboard.json); a newer date means the streak broke since the last publish, which is a notification. The note is for the notification and this session only; it is not on the page and must not go into the PR.

Step 4 - publish
Publish every run: the streak moves every run, so something always changed. git checkout -b scoreboard/<YYYY-MM-DD>, add only docs/scoreboard.json, commit as "Scoreboard for <YYYY-MM-DD>", push, and open a normal (non-draft) PR into main whose body is one line: the streak, and requests booked of total all time. Wait for the Tests check on the PR's head commit - poll every 30-60s, up to about 15 minutes; it installs Chrome, so it is slow. Do not wait on CodeRabbit. If Tests is green and the PR merges cleanly, squash-merge it. If Tests fails or it cannot merge, leave it open and do not touch anything else to fix it; set "ok" to false in scoreboard-row.json and add a "note" naming the PR and why it did not merge. If an earlier scoreboard PR is still open, leave it alone and mention it in the notification.

Step 5 - write the ledger row, notify if the rule above says to, and stop
gcloud storage cp --no-clobber scoreboard-row.json gs://gen-lang-client-0822973627-teetime-debug-artifacts/operations/scoreboard/<YYYY-MM-DD>.json

One retry, then report the failure rather than losing it. Then stop. Everything else is ask-first: if the derivation or a ledger row looks wrong, describe it in the session and the notification, and leave the fix to the maintainer.
