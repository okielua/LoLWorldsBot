# LoL Worlds Bot

A Discord bot for League of Legends Worlds.

## Current product priority

Communism's members consider pregame voting a central part of enjoying Worlds
together. Treat voting as the bot's main experience: discovering the daily polls,
making and changing picks, revealing votes, scoring results, and following the
server leaderboard. Schedules, team follows, reminders and timezone displays
support that experience.

Before deployment, prioritize a complete Discord check of that voting journey,
including button recovery after restart, the five-minute cutoff, persistent picks,
correct scoring, and isolation of test votes. Keep the agreed formatting and
notification semantics. Additional voting UX or timing changes should follow the
user's direction; this priority does not itself change when polls open or close.

## Planned features

- Match schedules and upcoming games
- Team-specific follows
- Five-minute public reminders
- Timezone-aware match times
- Pregame winner predictions
- Prediction scoring and server leaderboard
- Worlds standings
- Pick'ems-style predictions
- LoL Esports / Twitch Drops information
- AI `/ask` assistant
- Fun server-specific features

## Development

The bot is currently developed and run locally.

### Windows

From `C:\Users\luasd\DiscordStuff\LoLWorldsBot`:

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

The ignored `.env` file must contain `DISCORD_TOKEN` and `GUILD_ID`. Never commit
credentials. Commands synchronize to the configured guild.

## Live data and cache

At startup the bot loads `data/worlds_2026_cache.json`, or falls back to
`data/worlds_2026.json` if no usable cache exists. During Discord setup it refreshes
from `https://lolesports.com/api/gql` using the website's `homeEvents` query.

The client follows pagination and includes scheduled, live, and completed matches.
The adapter filters by the Worlds 2026 tournament ID and validates the snapshot
before atomically replacing the cache. Failed requests or invalid responses retain
the previous data and produce a warning in the logs. Unknown statuses never become
scheduled matches. Cache timestamps record fetch time, not Riot's last update time.

Team codes preserve existing SQLite follows; match IDs come from Riot. TBD slots
appear in schedules but cannot be followed and do not generate reminders.
Swiss standings are calculated from completed Swiss series results (one series
win/loss regardless of the Bo1/Bo3 game score), refreshed with each snapshot, and
corrected when source results change. Only known Swiss participants are listed.
No official rank/tiebreak order is invented. Explicit postponed/cancelled states
are not verified in this feed.

This is an undocumented website interface. The bot supports optional environment
overrides `LOLESPORTS_CLIENT_VERSION` and `LOLESPORTS_QUERY_HASH` if the site's
client version or persisted-query hash changes. Defaults were verified on
14 October 2026. The verified request requires no API key or browser cookies.

After startup, source refresh runs every five minutes, or every minute while a
match is live or its scheduled time is within one hour of now. Failures back off
from one minute up to 15 minutes away from matches; near/live matches retry every
minute. Source HTTP 429 responses respect `Retry-After` even when that is longer.
Access failures identify the client version/query hash to check in the logs. A
refresh that loses more than half the previously cached schedule is rejected.
The previous validated snapshot remains usable
for commands. Both background tasks wait for Discord readiness and stop on shutdown.

Reminder checks run independently every 15 seconds. They reload SQLite preferences
each time, skip TBD/non-scheduled matches, and pause
after a refresh failure or when the last successful fetch is over five minutes old.
DM reminders are deferred: their controls are hidden from `/setup` and automatic
polling does not generate DM events. Existing saved preferences are retained.
Public delivery is configured in Discord
through the private `/serversetup` panel, which requires **Manage Server** permission.
Choose a text channel where the bot has **View Channel** and **Send Messages**.
The first channel selection enables reminders; the panel also has an on/off toggle.
These server settings persist in SQLite and leave personal `/setup` unchanged.

One public message is sent per eligible match, even without ping recipients. Only
opted-in users are mentioned; everyone/role mentions are blocked. Public reminders
become eligible five minutes before scheduled start. Following downtime or a send
failure, unsent reminders can catch up until (but never at/after) match start, once
fresh source data is available. Catch-up wording reflects the actual remaining
minutes, with no seconds. Already delivered matches are not repeated, and missed
reminders for already-started matches are dropped rather than posted in a burst.

Transient send/database failures retry after 15, 30, then 60 seconds. Discord rate
limits respect the requested delay. Invalid channels or permission failures retry
at most once a minute and log the `/serversetup`/permission action needed. Retries
recheck eligibility, opt-ins, channel settings, and source freshness. A key
is consumed only after a successful send. SQLite's `server_reminder_deliveries`
table records the server ID, match ID, channel ID, Discord message ID, and UTC send
time. Its server/match key prevents repeats after normal restarts, channel changes,
and reschedules of an already delivered match. Startup creates the table without
changing existing user/server settings.

If receipt lookup fails, the bot skips the send. If Discord succeeds but saving the
receipt fails, the running bot suppresses resends and retries the receipt write on
each polling tick and shutdown. An abrupt crash after Discord accepts a message
but before SQLite commits can still lose that receipt. Discord and SQLite do not
share a transaction; this patch does not promise exactly-once delivery across that
gap, uncertain network failures where Discord accepted a send but its response was
lost, or multiple simultaneously running bot processes. Preserve the SQLite
file when deploying/restarting the bot.
Changed/removed matches clear preview keys, but do not clear successful public sends.

DM delivery remains deferred. Controlled Discord testing and deployment remain
pending. Source-side caching can delay updates;
a successful fetch does not prove the underlying schedule was just updated.

### Controlled Discord check

Restart the bot, open `/serversetup`, select the intended channel and turn public
reminders On. Click **Send Test Reminder (No Pings)**. This explicit admin action
posts one labelled simulated reminder through the real sender, records a separate
`test-reminder:` receipt in SQLite, then verifies that a fresh sender suppresses
a second attempt. Its result is private; the test never loads ping recipients or
changes real match data/receipts. A failed receipt write prevents the duplicate
check from sending another message. Verify the public message and private success
report in Discord before calling the end-to-end test complete.

### Shared presentation rules

These rules apply to every command, setup panel, component reply, and test report:

- Blue Components V2 cards by default, using the same normal-weight text renderer
  as the daily voting post. Only voting-result views retain native embeds for
  side-by-side columns (explicit user preference).
- One clear, underlined main title; matchups get emphasis, stage details stay muted.
- Public match schedules use Riot's `America/Los_Angeles` calendar date, once per
  day group. `/next`, `/schedule`, `/team`, `/today`, and `/tomorrow` do not require
  personal setup just to select the shared day. Without saved zones they show Los Angeles.
- Public messages list every saved setup timezone as `**Berlin:** 20:00`: bold city and colon,
  normal-weight time, no timezone abbreviation or seconds. Add a local date only
  if it differs from the Riot matchday. Show relative countdowns once per match,
  above the city list. City lists use small grey text with only city labels bold.
- Private match displays use only the recipient's saved timezone and its local
  calendar date. Without a saved timezone they use Pacific Time, labelled `11:00h (PT)`.
  `/myvotes` includes the local start time; `/changevote` and test history previews
  use the same rule. Date-only vote confirmations retain just the recipient's date.
- `/setup` includes a personal time-format dropdown: `1:00h (CEST)` or
  `1:00 am (CEST)`. It defaults to 24-hour time, persists across restarts, and
  applies to private messages only. Public city lists always use 24-hour time.
- Use status emojis consistently: 🕒 UPCOMING, 🔴 LIVE, 🏁 FINISHED, ⚠️ POSTPONED,
  and ⛔ CANCELLED. Scores and explanatory text stay normal-weight.
- Card help and page notes use small grey `-#` subtext. Voting results alone use
  the small native footer; its exact font weight is controlled by Discord.
- Voting results keep two inline team columns: bold team names, normal percentage
  and vote counts, plain `@name` text without pings, and blank space if no voters.
- Private vote confirmations show one date in the recipient's saved timezone
  (PT fallback), bold matchup teams, and the selected prediction as a heading.
- Setup, admin actions, `/myvotes`, and developer simulations remain private.
  Informational commands, daily voting posts, and real reminders remain public.
  Setup controls, reminder recipients, vote cutoffs, and scoring are unchanged.

### Daily predictions

Presentation preference: use normal-weight cards for new bot messages, with a blue
accent and approved underlined headings. Interactive voting posts use Components
V2 colored containers so buttons stay directly below their matchups. Private vote
confirmations bold both matchup teams and display the selected pick as a `##` heading.
Instruction footers in Components V2 cards use Discord's small, muted `-#` subtext
on each line.

One no-ping post contains every match in a Worlds matchday, with a separate pair
of team buttons directly below each matchup. Discord Components V2 makes the
interleaved layout possible in one message, including an eight-match Swiss day.
Blank spacing separates each match block from the preceding match's buttons.
Dates group by the US tournament matchday (`America/Los_Angeles`). Absolute match
times list every unique timezone saved through `/setup` as `City: HH:MM`.
A date suffix is included only when that city's date differs from the Riot
matchday in `America/Los_Angeles`. City-labelled lines omit the redundant timezone
abbreviation; DST conversion still uses the saved IANA zone. This also applies to
`/next`, `/schedule`, `/team`, and `/currentvotes`. Private `/myvotes` and `/changevote`
show only the recipient's local date/time (PT fallback). Private vote confirmations
keep a single date in that same timezone and no time or city list.
Relative countdowns appear only once. Public schedule headings group by Riot's
Los Angeles date. Existing saved zones are used immediately; invalid legacy zones
are ignored, and shared posts fall back to UTC if no one has selected a timezone.

`/today` and `/tomorrow` are public quick schedule lookups. Riot's Los Angeles
calendar date determines which day to select; all unique setup timezones are
shown for each matching game. Personal setup is not required to run them.
Matches of every status are
sorted by start time, with live/final scores where available and no poll results.
Empty days get a public no-matches response. Long days split across public embeds.
`/previous` publicly shows the two most recent completed series, newest first,
using the same Riot-date and city-time styling. Scores and winner-only results
are wrapped in Discord spoiler tags; matchups, times and stage labels stay visible.
Live, scheduled, postponed and cancelled matches are excluded. Fewer than two
completed matches are handled without inventing results. Recency follows start
time because the source model does not provide exact match-end timestamps.
Prediction boards are posted as soon as the source confirms a scheduled matchup
before its cutoff, with no fixed hour or lead-time gate. The existing daily card
layout supports an eight-match Swiss day within Discord's 40-component limit.
The title is Worlds Predictions, so advance boards are clearly dated rather than
labelled today. TBD pairings are omitted until confirmed. Newly announced pairs
are added in place, retaining existing polls, votes, and stable component IDs.
Source refresh immediately wakes the voting loop; independent 15-second checks
continue to lock polls even between refreshes. Missing/deleted active boards are
replaced without deleting prediction rows; the new message receipt is persisted.
Active boards are checked on restart and every five minutes, even when their
content has not changed, so deletion does not leave voting unavailable indefinitely.

Votes and changes receive private confirmations. `/currentvotes` targets the next
scheduled match, live matches, and completed polls whose result was first observed
within the last hour. It never discloses an open pick, count, percentage, or voter.
Matches are grouped into one Discord response with a separate card per match.
Voting results retain their side-by-side team columns. Extra messages are used
only if Discord's combined text, embed, or component limits require a split.
Public result details require both a locked poll and a successful five-minute
reminder receipt. Until then it shows only a privacy notice and match information.
It never pings voters and has no match selector. Status, date and city
times appear below the two voting columns. The completion window
is stored in SQLite and does not restart on refresh, result corrections, or bot
restarts. The source model has no exact finish timestamp, so this hour is measured
from the first fresh completed result the bot observes, rather than match start.
The daily post and leaderboard do not list open picks. Each match's database cutoff is
exactly five minutes before scheduled start. Buttons are disabled on the next
15-second update, and the reminder path also updates the controls before sending.
Late clicks are rejected by time even if Discord hasn't displayed disabled buttons
yet. The regular reminder includes both teams' percentages, vote counts, and voter
names. Only opted-in reminder recipients are pinged; voter names themselves do not
create additional pings.

SQLite stores match polls, editable predictions, daily message IDs, and results.
`/changevote` privately reopens the caller's most recently edited pick if that poll is
still open; otherwise it offers the earliest available open poll. Its two team
buttons use the same save/change path and exact five-minute cutoff as daily posts.
It offers only polls with a confirmed publication record. If no poll is open, it
links the user to `/today` and `/tomorrow`. Confirmation cards include small grey
help for `/changevote`, `/currentvotes`, and `/leaderboard`.
During an active controlled prediction test, `/changevote` targets the isolated test
manager and labels its reply TEST. When that test finishes or fails, it switches
back to the live tournament. This never copies test votes into live scoring.
`/leaderboard` is public and ranks current server members by correct series
predictions, then accuracy, then submitted count, then stable Discord user ID.
Equal correct/accuracy/submitted figures share a rank. It shows correct/submitted,
accuracy, submitted/eligible matches, participation, current streak, and best streak.
User-facing statistics are called correct predictions, never points. An active
streak gets a fire marker only at three or more. No extra achievement posts are sent.

### Eligibility, corrections, and match-day MVP

Status labels share one icon set in `services/status_labels.py`: 🗳️ for open
voting/active predictions, 🔒 for closed voting awaiting results, 🏁 for past
predictions/final matches, ✅ for correct predictions, ❌ for incorrect predictions,
⚠️ for changed or postponed matches, and ⛔ for cancellations. Correct/incorrect
outcomes and match scores remain behind spoilers; lifecycle labels are visible.

An eligible completed match has a confirmed winner and a known-team poll that was
successfully sent/edited in Discord strictly before its five-minute cutoff. A
created database row or failed Discord send alone is not an opportunity. The first
acknowledged publication time is retained, including when a delayed database write
is retried. Legacy migrations infer that time only where an existing saved vote
proves an opportunity; unknown historical opportunities are excluded conservatively.

Accuracy is correct/submitted on eligible completed matches. Participation is
submitted/all eligible completed matches. A missed prediction reduces participation
but is not an incorrect prediction. Pending, cancelled, unknown-winner and unpublished
matches do not affect these denominators. Streaks use submitted scored predictions
in start-time order (stable match ID for simultaneous starts): correct extends,
incorrect resets, missed and unresolved predictions are skipped until resolved.
All figures are derived from SQLite results each time, so repeated refreshes cannot
double-score and corrected official winners recompute totals and streaks.

The first vote time is preserved separately from the latest update. Each vote also
records the two teams it referred to. Changed pairings retain the old votes for
history but invalidate their scoring until the user submits a valid new choice.
An unclosed poll follows the refreshed start time; once locked it never reopens.
Predictions at/after a revised cutoff do not score. Changing pairings resets the
publication proof so a newly confirmed matchup needs its own acknowledged display.

Daily cards include a compact Previous match-day MVP recap once the previous Riot
match day's non-void matches all have confirmed completed results. Rest days do
not matter. Most correct wins, accuracy breaks ties, and all joint winners appear.
The recap includes correct/submitted and submitted/eligible participation. It is
omitted for missing/incomplete results or no correct predictions, and never delays
posting new polls. Test tournaments remain isolated from the real competition.

`/pickems` (description: `goto pickems leaderboard`) links to the official Riot
group URL set with **Set Pick'ems Link** in `/serversetup`. It stays separate from
the Discord prediction leaderboard. Daily posts and controls are restored from
SQLite after ordinary restarts. The same Discord-acknowledgement/SQLite-commit
crash gap described for reminders also applies to creating a daily post.

### Controlled prediction check

In `/serversetup`, click **Test Daily Predictions (No Pings)** with public reminders
On. It posts an isolated, labelled test day with two matchups. Vote or change picks
before the cutoffs (about one and two minutes after the click). Two no-ping test
reminders reveal the results, then simulated winners produce a private test score
report. Keep the bot running for about two minutes. Test records use their own
tournament ID and do not affect the real `/leaderboard` or real match receipts.
If the admin running the test votes, the test also sends them a private TEST
prediction-history preview with simulated series scores and correct/incorrect
outcomes behind spoiler tags, using the same layout as `/myvotes`.

`/myvotes` privately shows the caller's current-tournament predictions in this
server, including correct/incorrect outcomes once a winner is recorded. It has
no user selector and includes open picks without exposing anyone else's votes.
Long histories are split across private cards. Voting results on reminders
use a blue embed with side-by-side bold team names, normal-weight percentages
and vote counts in the payload, plus a small native two-line `/currentvotes` and
`/leaderboard` footer. Native embed font weight depends on the Discord client;
these result columns are the approved exception to the normal-weight card rule.
The matchup reminder stays inside the embed;
the matchup is the title, timing comes next in small subtext, and an underlined Voting Results
section introduces the columns. Simulation labels remain outside the embed.
opted-in pings stay in message content so Discord delivers notifications.
grant the bot **Embed Links** in the reminder channel. Without
that permission reminders fall back to plain text.

Deployment remains deferred until the new prediction UI is tested in Discord.

To manually fetch and validate the cache without starting Discord:

```powershell
python scripts/refresh_tournament.py
```

For read-only verification of pagination, historical results, and any live matches:

```powershell
python scripts/verify_lolesports.py
```

The scripts use the verified default client settings. The permanent historical
fixture `data/worlds_2025.json` remains unchanged.

## Tests

```powershell
python -m py_compile bot.py
python -m pytest -q
```

Pytest uses mocked network responses and temporary settings databases. It compiles
project Python without starting Discord and tests parsing, pagination, cache
recovery, reminder eligibility, queries, and command formatting.
