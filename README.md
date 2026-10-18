# LoL Worlds Bot

A Discord bot for League of Legends Worlds.

## Development and Production

Set `BOT_MODE=production` in a separate environment file to hide developer
commands and setup simulations, isolate the live database/cache, and require
verified schedule data. Existing `.env` defaults to development and retains the
current database and test features. Use a separate Discord application/token
for production. See [deployment instructions](deploy/README.md) and the example
environment files. The VPS service and backup helper are provided in `deploy/`.

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

For a quick restart, double-click `Restart Bot.cmd` in this folder, or run
`.\restart-bot.ps1` in PowerShell. It stops this project's existing bot and starts
the updated code in the background. Each run writes logs in the ignored `.logs/`
folder. If running manually in a terminal, Ctrl+C, Up Arrow, Enter reruns the
previous command without using the clipboard.

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
DM reminders are deferred: their controls are hidden from `/mysetup` and automatic
polling does not generate DM events. Existing saved preferences are retained.
The private `/mysetup` card deletes itself after ten minutes without activity.
Using a control refreshes the timer; open `/mysetup` again to start a new session.
Personal setup is required before using other commands or bot controls, including
public voting buttons and restart-restored buttons. New members receive a private
prompt to run `/mysetup`; autocomplete stays empty until then. Successfully
presenting `/mysetup` records access per server/member, without requiring optional
team selections. Previous role-onboarding records migrate as existing setups.
Setup controls remain accessible, and restarting does not reset setup status.
Public delivery is configured in Discord
through the private `/serversetup` panel, which requires the **Manage Server** permission, regardless of role names. Permission is checked again when using its controls or submitting its forms.

In `/serversetup`, an Admin selects an existing Worlds notification role from
the role dropdown. Role creation is not part of setup. Select a role without
server permissions, since members can obtain it themselves. Selecting the role
does not require the bot to have Manage Roles. To let members get or remove it,
the bot needs **Manage Roles**, with its own role above the selected role. Members can get or remove
this role using **Server Pings for All Games** in `/mysetup`. Actual role
membership is the subscription state, including manual Discord role changes.
The first time a member opens `/mysetup` with a configured and assignable role,
the bot subscribes them automatically. A persistent per-server onboarding record
prevents later visits from reversing an opt-out. Missing roles or failed role
assignment do not mark onboarding complete. Existing members are not bulk-added.
There is no separate daily-post subscription. The legacy SQLite all-games flag
is retained for compatibility but no longer targets notifications.
New daily prediction posts (including manual publications and replacements of deleted posts) and five-minute match reminders mention
this role. Edits and restarts do not ping it. The role is
required to be mentionable unless the bot has Mention Everyone permission.
Followed-team subscribers outside this role receive individual daily voting
and five-minute match pings for their teams;
role members are not also individually mentioned. With no configured role,
only followed-team subscribers are individually pinged.
Newly confirmed matchups added to an existing board send a short follower
notification linking back to that board. These later notices target matching
followers even if they already have the Worlds role, since the role itself is
not pinged again on edits. Persistent matchup receipts prevent restart replays;
upgrades baseline already-published matchups instead of sending old notices.
Choose a text channel where the bot has **View Channel** and **Send Messages**.
The first channel selection enables reminders; the panel also has an on/off toggle.
These server settings persist in SQLite and leave personal `/mysetup` unchanged.

One public message is sent per eligible match, even without ping recipients. Only
the configured Worlds role and opted-in followed-team users are mentioned;
everyone and unrelated roles are blocked. Public reminders
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
  day group. `/nextgames`, `/schedule`, `/team`, `/today`, and `/tomorrow` do not require
  personal setup just to select the shared day. Without saved zones they show Los Angeles.
- Public messages list every saved setup timezone as `**Berlin:** 20:00`: bold city and colon,
  normal-weight time, no timezone abbreviation or seconds. Add a local date only
  if it differs from the Riot matchday. Show relative countdowns once per match,
  above the city list. City lists use small grey text with only city labels bold.
- Private match displays use only the recipient's saved timezone and its local
  calendar date. Without a saved timezone they use Pacific Time, labelled `11:00h (PT)`.
  `/myvotes` includes the local start time; `/changevote` and test history previews
  use the same rule. Date-only vote confirmations retain just the recipient's date.
- `/mysetup` includes a personal time-format dropdown: `1:00h (CEST)` or
  `1:00 am (CEST)`. It defaults to 24-hour time, persists across restarts, and
  applies to private messages only. Public city lists always use 24-hour time.
- Use status emojis consistently: 🕒 UPCOMING, 🔴 LIVE, 🏁 FINISHED, ⚠️ POSTPONED,
  and ⛔ CANCELLED. Scores and explanatory text stay normal-weight.
- Card help and page notes use small grey `-#` subtext. Voting results use a
  full-width subtext field below the columns, preserving inline-code commands.
- Voting results keep two inline team columns: bold team names, normal percentage
  and vote counts, plain `@name` text without pings, and blank space if no voters.
- Private vote confirmations show one date in the recipient's saved timezone
  (PT fallback), bold matchup teams, and the selected prediction as a heading.
- Setup, admin actions, `/myvotes`, and developer simulations remain private.
  Informational commands, daily voting posts, and real reminders remain public.
  Setup controls, reminder recipients, vote cutoffs, and scoring are unchanged.

### Daily predictions

Presentation preference: use normal-weight cards for new bot messages, with a blue
accent. Use standard title case for fixed titles and section headings throughout
the bot, preserving proper names such as PayPal. Keep articles and short
prepositions lowercase inside titles (for example, Democracy Was a Mistake).
Preserve user/team names and acronyms exactly; matchup vs remains lowercase.
Retain approved underlined headings. Interactive voting posts use Components
V2 colored containers so buttons stay directly below their matchups. Private vote
confirmations bold both matchup teams and display the selected pick as a `##` heading.
Instruction footers in Components V2 cards use Discord's small, muted `-#` subtext
on each line.

One no-ping post contains every match in a Worlds matchday, with a separate pair
of team buttons directly below each matchup. Discord Components V2 makes the
interleaved layout possible in one message, including an eight-match Swiss day.
Blank spacing separates each match block from the preceding match's buttons.
Dates group by the US tournament matchday (`America/Los_Angeles`). Absolute match
times list every unique timezone saved through `/mysetup` as `City: HH:MM`.
A date suffix is included only when that city's date differs from the Riot
matchday in `America/Los_Angeles`. City-labelled lines omit the redundant timezone
abbreviation; DST conversion still uses the saved IANA zone. This also applies to
`/nextgames`, `/schedule`, `/team`, and `/currentvotes`. Private `/myvotes` and `/changevote`
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

`/today` also provides team prediction buttons beneath eligible known matchups,
with private confirmations and the same five-minute cutoff. Its three small grey
help lines link to `/changevote`, `/currentvotes`, and `/leaderboard`. These public
buttons retain their matchup snapshot across restarts, reject changed pairings
and late clicks, and reuse existing poll/vote rows. Only acknowledged Discord
responses count as published opportunities; failed sends do not. Completed, live,
unknown-team, and closed matches do not get active voting buttons. Long days split
within Discord's message/component limits. `/tomorrow` remains informational.

`/previousgames` publicly shows the three most recent completed series by default, newest first,
using the same Riot-date and city-time styling. Scores and winner-only results
use the global timed spoiler policy described below; times and stage labels stay visible.
Live, scheduled, postponed and cancelled matches are excluded. Fewer than two
completed matches are handled without inventing results. Recency follows start
time because the source model does not provide exact match-end timestamps.
New prediction boards are limited to the next unfinished tournament matchday:
every earlier matchday must have all its matches completed or cancelled first.
An earlier day with unknown opponents, a live game, or a postponed game blocks
later days. The next day's board appears when fresh data confirms at least one
scheduled matchup before its voting cutoff, and it is within 24 hours of that
matchday's first non-cancelled game (including TBD matchups). Aim for at least
eight hours of actual voting time before the five-minute cutoff by publishing
as soon as those conditions allow. If earlier games finish late or teams are
confirmed late, publish immediately rather than skipping the shorter window.
There is no fixed posting hour. Existing boards are retained after rescheduling;
any new board for a moved matchday still obeys the 24-hour limit.
Existing boards continue updating. The daily card supports an eight-match Swiss
day within Discord's 40-component limit. TBD pairings are omitted until confirmed. Newly announced pairs
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
`/dailyvote` privately links to this server's latest published daily voting post and
shows its matchday date. It does not create another post or send notifications.
Before any daily post has been published, it directs members to `/today`.

Successful vote-button interactions also send a short participation message in
the `/serversetup` reminder channel: `🗳️ @name just cast a vote on:`, followed by
the matchup on its own large heading with team emojis and centered tags, then
the match's tournament date in parentheses.
An accompanying footnote gives the voting time remaining when posted (up to the
five-minute-before-start cutoff), with `/dailyvote` and `/changevote` instructions.
The selected team stays private, participant spoiler protection still applies,
and all user/role/everyone pings are disabled. Controlled prediction simulations
send the same announcements with a clear TEST label. Missing channels or failed announcements never
undo a saved vote or prevent its private confirmation.

Members with **Manage Server** can use `/serversetup` → **Publish Daily Post**
or **Delete Daily Post**. Manual publishing creates or refreshes the earliest
unfinished matchday's board, even if automatic reminders are off. New posts ping
the configured role; refreshing an existing board does not send another ping.
It respects fresh-source checks, the 24-hour lead limit, previous-matchday
completion, known opponents, and the five-minute voting cutoff. Repeated clicks
reuse the existing board. Deleting preserves polls/votes and persists a pause for
that matchday, preventing automatic recreation after restarts. Publishing again
clears that pause. Future matchdays retain their normal automatic behavior.

The daily post and leaderboard do not list open picks. Each match's database cutoff is
exactly five minutes before scheduled start. Buttons are disabled on the next
15-second update, and the reminder path also updates the controls before sending.
Late clicks are rejected by time even if Discord hasn't displayed disabled buttons
yet. The regular reminder includes both teams' percentages, vote counts, and voter
names. Only opted-in reminder recipients are pinged; voter names themselves do not
create additional pings.

SQLite stores match polls, editable predictions, daily message IDs, and results.
`/changevote` privately lists every published, currently open match in chronological
order, with independent team buttons below each matchup. Saved selections are green
and checked; changing one immediately updates its buttons without changing other
predictions. Long lists split into cards within Discord's limits. The buttons use
the same save/change path and exact five-minute cutoff as daily posts. If no poll is open, it
links the user to `/today` and `/tomorrow`. Confirmation cards include small grey
help for `/changevote`, `/currentvotes`, and `/leaderboard`.
During an active controlled prediction test, `/changevote` targets the isolated test
manager and labels its reply TEST. When that test finishes or fails, it switches
back to the live tournament. This never copies test votes into live scoring.
`/leaderboard` is public and ranks current server members by correct series
predictions, then accuracy. Equal correct/accuracy figures share a competition
rank (1, 2, 2, 4); Discord user IDs only stabilize display order within ties.
The leaderboard uses the normal-weight Components V2 card renderer, with an
underlined title and third-level player headings containing a shaded inline-code
rank, name, correct count, and movement. Stats follow on the very next line, with
one blank line between players. Supporting stats use thin muted subtext, with
the accuracy percentage and word correct explicitly bold. Current streaks of
three or more show a fire emoji. Leader-gap and level-on-correct lines are omitted.
Small grey text is italic without bullets. Only actual footer/help notes have
bullets; explanatory text is unbolded and command names
retain inline-code formatting. Larger leaderboards use Previous/Next buttons
while keeping one card visible. The My stats button stays below the card contents.
There are no fire markers or participation percentages in this view.

Movement compares current ranks with the rankings before the most recently
confirmed eligible result was included. SQLite's persisted first-confirmation
timestamps determine that result (start time and match ID break batch ties).
The comparison is reconstructed from results, never from leaderboard visits,
so repeated requests and restarts do not reset movement. A newly ranked player
shows New; unchanged ranks have no arrow. Unknown legacy comparison data yields
no movement.

The blue **My Advanced Stats** button and `/myvotestats` use the same private handler and database
snapshot, scoped to the leaderboard's server and tournament. The button always
shows the clicking user's data and survives restarts. Personal stats use a first-level
My Worlds Stats title, My rank/correct predictions, and summary lines for My votes,
My incorrect/missed predictions, and My streaks. These lines have no bullets.
Four second-level section headings use the server's PeyzPal, PepeKMS, Copium,
and Discheesed custom emojis in that order, without an Advanced stats heading.
Explanations are italic grey subtext; the introductory scope note is also in
parentheses. A muted scope note states that these stats
cover completed series from Swiss Stage onward. The leaderboard uses that same
stage filter for totals, ranks, movement, team insights, and crowd statistics.
Play-ins and unknown stages are excluded; stored votes are preserved. Missing
rank/accuracy use the explicitly approved dash, with clear no-data values under
each section. The sample leaderboard's My Advanced Stats button still
opens the clicker's actual private statistics; sample scores are never stored.

The four personal advanced sections are **Check Your PayPal** (best-read team),
**Can’t Read These Frauds** (hardest team), **Hopium Supplier** (most losing backings),
and **Democracy Was a Mistake** (minority accuracy). Each has a grey explanation
and its actual value on a separate line. Recent/pending lists, the boldest-call
section, and trailing help footnotes are omitted from this layout. These stats use
only validated votes on eligible completed matches, from the same SQLite read
snapshot as the totals. Team accuracy counts predictions involving that team,
whether for or against it, and needs at least three scored predictions. Equal
accuracies prefer the larger sample, then team ID. Hopium Supplier counts completed
series where the user picked a team to win and that team lost, and shows the team
with the highest count. Wins, votes against that team, and unscored predictions do
not increase this count. Ties use team ID. If there are no losing backings, no team
is selected.

Crowd counts include every valid final vote in this server/tournament, including
the requester's vote. Strictly less than half is a minority; even splits are
excluded. The underlying snapshot still computes the optional boldest-call metric,
but it is not shown in the current personal layout. Pending, cancelled,
unpublished, late, and changed-pair votes cannot inflate these stats.
Insufficient data gets an explicit explanation instead of an invented result.

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

Daily cards include a compact Previous Match-Day MVP recap once the previous Riot
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

For an immediate populated standings preview, use `/serversetup` and click
**Preview Leaderboard**. It privately shows six clearly labelled sample players,
including tied ranks, accuracy, participation, and streaks, using the real leaderboard
renderer. It works without enabling public reminders and writes no competition data.

In `/serversetup`, click **Test Daily Predictions (No Pings)** with public reminders
On. It posts an isolated, labelled test day with two matchups. Vote or change picks
before the cutoffs (about one and two minutes after the click). Two no-ping test
reminders reveal the results, then simulated winners produce a private test score
report. Keep the bot running for about two minutes. Test records use their own
tournament ID and do not affect the real `/leaderboard` or real match receipts.
If the admin running the test votes, the test also sends them a private TEST
prediction-history preview with simulated series scores and correct/incorrect
outcomes behind spoiler tags, using the same layout as `/myvotes`.

`/votehistory voter` requires a server member selected from autocomplete, limited
to members who have voted in the current tournament. It privately displays their
complete history in pages of five predictions, keeping picks hidden until voting
closes. The **View Advanced Stats** button opens the same stats display as
`/myvotestats`, calculated for the selected voter.

`/myvotes` privately shows the caller's current-tournament predictions in this
server, including correct/incorrect outcomes once a winner is recorded. It has
no user selector and includes open picks without exposing anyone else's votes.
Underlined matchups are grouped under Past Games, Ongoing Games when applicable,
and Upcoming Games. A completed prediction shows its spoilered correct/incorrect
label beside the user's pick, using the server's KingPeepo or peepoSad emoji when
available. Each match includes its stage and best-of format. Result spoilers expire
under the global policy; neither team receives result-revealing winner emphasis.
Each card ends with the shared three-line prediction help in muted subtext.

### Global spoiler policy

The production renderer applies the same policy to public replies, private
replies, reminders, voting cards, histories, standings and prediction statistics.
Live scores are omitted entirely. Completed series scores, winner/correctness
information and affected aggregates are spoiler-tagged until the earlier of
14 hours after completion or 2 hours before the earliest known connected series.
Connections include either team's next fixture and recorded participant-slot
prerequisites. Without a known connection, the 14-hour deadline applies.

The schedule API has no trustworthy end timestamp or explicit bracket graph.
`spoiler_ends` therefore persists the first observed completion time, reusing
existing `result_observed_at` timestamps when available. This intentionally errs
on the side of keeping results protected longer after downtime. `spoiler_slots`
tracks each participant independently. Newly disclosed participants inherit their
most recent preceding completed series as a prerequisite; slots already known
before that completion stay independently visible. Unknown prerequisite results
fail closed. A newly announced connected fixture can shorten the release deadline.

Team displays use existing custom emoji before centered, fixed-width inline-code
abbreviations. Centering uses NBSP and places an odd extra space on the right.
The field width is the longest abbreviation (at least four characters) plus two
spaces, so even the longest tag has a margin on both sides.
Each protected participant's emoji and padded name share a single spoiler.
Protected vote buttons say **[see spoiler]** with the server's
`beeQuestion` emoji (Unicode question mark fallback if unavailable), preserving
the original vote IDs. Left/right button order matches left/right fixture order.
Successful private vote confirmations reveal the actual matchup and selected
team without spoilers or placeholders; public cards and controls remain protected.
Other independently known teams,
dates, start times, stage labels and series formats remain visible.

Swiss rows use alphabetical order, with affected records concealed. Prediction
rankings conceal the complete affected body, including names/rank order, rather
than exposing a winner through ordering or highlighting. Personal advanced stats
keep headings, explanations and labels readable and protect only affected values.
Their dependencies come from the actual eligible Swiss-onward results and the
member's scored predictions; unrelated Play-in results do not hide an empty card.
Gol.gg aggregates are protected too; aggregate details are withheld while a series
is live. Team-filtered history omits a future fixture if the queried team's very
presence would be a spoiler. Targeted follower mentions also exclude hidden
participants so the recipient list cannot disclose an unannounced team.

`spoiler_messages` stores non-ephemeral draft content/components, not interaction
tokens. A 15-second release worker silently edits existing messages through the
bot's channel API; it neither reposts nor pings. Tracking and completion times
survive restarts. A one-time migration adopts editable bot messages from the
preceding 24 hours, including old daily cards; inaccessible channels are logged.
Old ephemeral replies cannot be recovered or reliably edited after token expiry:
users rerun the command to refresh them. Existing historical messages outside
that migration window are not rewritten. The Discord REST and interaction-webhook
guards are covered against the pinned discord.py version; keep these transport
tests when upgrading the library.
Missing custom emojis retain their
named shortcode instead of substituting a different emoji.
Long histories are split across private cards. Voting results on reminders
use a blue embed with side-by-side bold team names, normal-weight percentages
and vote counts in the payload, plus small grey two-line `/currentvotes` and
`/leaderboard` help in a full-width Markdown field. Native footer text is not used,
so both commands retain inline-code formatting. Native embed font weight depends on the Discord client;
these result columns are the approved exception to the normal-weight card rule.
The matchup reminder stays inside the embed;
the matchup is the title, timing comes next in small subtext, and an underlined Voting Results
section introduces the columns. Simulation labels remain outside the embed.
opted-in pings stay in message content so Discord delivers notifications.
grant the bot **Embed Links** in the reminder channel. Without
that permission reminders fall back to plain text.

Production deployment files are prepared in `deploy/`. Deployment itself still
requires the live Discord application's credentials and access to the VPS.

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

`/nextgames [count]` and `/previousgames [count]` accept an optional whole-number count from 1 to 100 (default 3). Results use pages of three matches with Previous/Next buttons. A **See 3 More Next Matches** or **See 3 More Previous Matches** button expands the current results and opens the next batch without sending another message. The button disappears when all available matches (up to 100) are loaded, and the title updates to the loaded total. Only scheduled future matches appear in `/nextgames`; only completed matches appear in `/previousgames`. Requests exceeding the available fixtures show all available matches with an explanatory note.
