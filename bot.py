import logging
import asyncio
import os
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from services.presentation import embed_card_payload, message_payload
from services.team_emojis import configure_team_emojis, team_emoji
from discord import app_commands

from models import Match, MatchStatus
from services.delivery_records import initialize_delivery_records
from services.background_polling import BackgroundPolling
from services.pickems_reminder import PickemsReminder
from services.live_tournament import LiveTournamentStore, TBD_TEAM_ID
from services.tournament_sources import MultiSourceClient
from services.lolesports import LoLEsportsClient, QUERY_HASH
from services.server_reminders import ServerReminderDelivery
from services.server_settings import initialize_server_settings
from services.server_setup import ServerSetupView, build_server_setup_content, can_manage_server
from services.worlds_role import configured_worlds_role, worlds_subscription_enabled, toggle_worlds_subscription, initialize_worlds_subscription
from services.polls import get_user_votes, get_upcoming_match_votes, get_recent_completed_match_ids, has_match_votes, initialize_polls, get_latest_daily_post
from services.prediction_display import PREDICTION_BLUE, VOTE_CONFIRMATION_HELP, group_voting_messages, hidden_votes_payload, my_votes_embeds, upcoming_votes_embed
from services.match_times import TOURNAMENT_TIMEZONE, format_clock_time, format_timezone_times, private_match_details
from services.user_settings import get_registered_timezones
from services.server_settings import get_server_settings
from services.voting import DailyPageButton, ScheduleVoteButton, VotingManager, personal_vote_payloads, schedule_vote_payloads, my_vote_payloads, current_votes_controls
from services.prediction_stats import get_competition_stats
from services.leaderboard_views import MyStatsButton, leaderboard_context, leaderboard_payload, show_personal_stats
from services.status_labels import MATCH_STATUS_LABELS
from services.standings_display import standings_text
from services.golgg import GOLGG_FOOTNOTES, team_stats_text
from services.golgg_players import player_catalog, player_reply, refresh_players_loop
from services.team_history import HistoryPages, TeamHistoryView
from services.match_browser import show_match_games
from services.vote_history import voter_autocomplete, show_vote_history
from services.vote_panel import OpenVotePanel, PublicVotersButton, daily_post_reference
from services.runtime_config import runtime_config, prepare_seed, remove_development_commands, validate_application
from services.setup_gate import SetupCommandTree
from services.spoilers import SpoilerLedger, current_policy, date_heading_token, matchup, score_token
from services.spoiler_delivery import SpoilerDelivery
from services.user_settings import get_connection
from services.user_settings import mark_setup_completed
from services.reminder_scheduler import check_reminders
from services.reminder_engine import (
    ReminderTracker,
    get_server_ping_user_ids,
)
from services.timezones import TIMEZONE_REGIONS
from services.timezone_search import CitySearchButton
from services.tournament_data import load_tournament_data
from services.tournament_queries import (
    get_previous_matches,
    get_matches_for_local_date,
    get_matches_for_votes,
    get_matches_for_stage,
    get_matches_for_team,
    get_next_match,
    get_standings,
)
from services.user_settings import (
    get_all_user_reminder_preferences,
    get_followed_team_ids,
    get_notification_settings,
    get_user_timezone,
    get_user_time_format,
    initialize_database,
    set_followed_team_ids,
    set_server_ping,
    set_user_timezone,
    set_user_time_format,
)

# --------------------------------------------------
# Configuration
# --------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing from .env")

GUILD_ID = int(GUILD_ID)


# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("worldsbot")


# --------------------------------------------------
# Tournament data
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "worlds_2026.json"

tournament_store = LiveTournamentStore(
    prepare_seed(load_tournament_data(DATA_FILE)),
    runtime_config().cache_path,
    MultiSourceClient(LoLEsportsClient(
        client_version=os.getenv("LOLESPORTS_CLIENT_VERSION", "4078aa9"),
        query_hash=os.getenv("LOLESPORTS_QUERY_HASH", QUERY_HASH),
    ), cache_path=runtime_config().cache_path),
)
worlds = tournament_store.data

initialize_database()
initialize_server_settings()
initialize_delivery_records()
initialize_polls()

logger.info(
    "Loaded %s with %s matches",
    worlds.tournament.name,
    len(worlds.matches),
)


# --------------------------------------------------
# Time helpers
# --------------------------------------------------


def local_datetime(
    dt: datetime,
    timezone_name: str,
) -> datetime:
    return dt.astimezone(ZoneInfo(timezone_name))


def format_local_date(
    dt: datetime,
    timezone_name: str,
) -> str:
    local = local_datetime(
        dt,
        timezone_name,
    )

    return f"{local.day} {local.strftime('%B %Y')}"


def format_local_time(
    dt: datetime,
    timezone_name: str,
) -> str:
    local = local_datetime(
        dt,
        timezone_name,
    )

    timezone_abbreviation = local.tzname() or timezone_name

    return format_clock_time(local, timezone_abbreviation)


def labels_are_duplicate(
    stage_name: str,
    round_name: str,
) -> bool:
    def normalize(value: str) -> str:
        return value.lower().replace(" ", "").replace("-", "").rstrip("s")

    return normalize(stage_name) == normalize(round_name)


def display_stage_name(name: str) -> str:
    return "Play-ins" if name in {"Play-In", "Play-Ins"} else name


def format_relative_time(dt: datetime, now: datetime) -> str:
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "in less than a minute"

    hours, minutes = divmod(ceil(seconds / 60), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return f"in {' '.join(parts)}"


# --------------------------------------------------
# Match formatting
# --------------------------------------------------


def format_match(
    match,
    timezone_name: str,
    include_date: bool = True,
    now: datetime | None = None,
    timezones: list[str] | None = None,
    hide_result: bool = True,
    small_details: bool = True,
    include_stage: bool = True,
    date_as_heading: bool = True,
) -> str:
    team1 = worlds.teams_by_id[match.team1_id]

    team2 = worlds.teams_by_id[match.team2_id]

    stage = worlds.stages_by_id[match.stage_id]

    local_date = format_local_date(
        match.start_time,
        TOURNAMENT_TIMEZONE,
    )

    relative_time = ""
    if match.status == MatchStatus.SCHEDULED:
        relative = format_relative_time(match.start_time, now or datetime.now(UTC))
        if relative:
            relative_time = f" ({relative})"

    parts = []

    if include_date and date_as_heading:
        parts.append(f"## __{date_heading_token(match.start_time, TOURNAMENT_TIMEZONE, now=now)}__")

    stage_details = [display_stage_name(stage.name)] if include_stage else []

    if match.round_name and not labels_are_duplicate(
        stage.name,
        match.round_name,
    ):
        stage_details.append(match.round_name)

    stage_details.append(f"Bo{match.best_of}")

    stage_line = ""
    if not (include_date and not date_as_heading):
        stage_line = ('-# ' if small_details else '') + ' · '.join(stage_details) + '\n\n'
    times = format_timezone_times(match.start_time, timezones or [timezone_name])
    if small_details:
        times = "\n".join(f"-# {line}" for line in times.splitlines())
    countdown = f"-# {relative_time.strip()}\n" if relative_time else ""
    date_detail = f"-# {local_date} · Bo{match.best_of}\n\n" if include_date and not date_as_heading else ""
    pairing = matchup(match) if current_policy() else f"{team1.short_name} vs {team2.short_name}"
    parts.append(f"## {pairing}\n{stage_line}{date_detail}{countdown}{times}")

    if match.status == MatchStatus.COMPLETED:
        if current_policy():
            parts.append(f"{MATCH_STATUS_LABELS[match.status]}\n### Result: {score_token(match)}")
            return "\n".join(parts)
        if match.team1_score is not None and match.team2_score is not None:
            first = f"**__{team1.short_name}__**" if match.winner_id == match.team1_id else f"**{team1.short_name}**"
            second = f"**__{team2.short_name}__**" if match.winner_id == match.team2_id else f"**{team2.short_name}**"
            result = f"{first} **{match.team1_score}–{match.team2_score}** {second}"
        elif match.winner_id:
            result = f"Winner: {worlds.teams_by_id[match.winner_id].short_name}"
        else:
            result = "Result unavailable."
        if hide_result:
            result = f"||{result}||"
        parts.append(f"{MATCH_STATUS_LABELS[match.status]}\n### Result: {result}")

    elif match.status.value == "live":
        if current_policy():
            parts.append(MATCH_STATUS_LABELS[match.status] + " · Scores hidden until the series finishes")
            return "\n".join(parts)
        if match.team1_score is not None and match.team2_score is not None:
            result = (
                f"{team1.short_name} "
                f"{match.team1_score}"
                f"–{match.team2_score} "
                f"{team2.short_name}"
            )
            if hide_result:
                result = f"||{result}||"
            parts.append(f"{MATCH_STATUS_LABELS[match.status]} · {result}")

        else:
            parts.append(MATCH_STATUS_LABELS[match.status])

    elif match.status.value == "postponed":
        parts.append(MATCH_STATUS_LABELS[match.status])

    elif match.status.value == "cancelled":
        parts.append(MATCH_STATUS_LABELS[match.status])

    else:
        parts.append(MATCH_STATUS_LABELS[match.status])

    return "\n".join(parts)


def format_team_matches(matches, timezones, now=None):
    lines = []
    previous_stage = None
    for match in sorted(matches, key=lambda item: (worlds.stages_by_id[item.stage_id].order, item.start_time, item.id)):
        if match.stage_id != previous_stage:
            stage = worlds.stages_by_id[match.stage_id]
            lines.append(f"## __{display_stage_name(stage.name)}__")
            previous_stage = match.stage_id
        lines.append(format_match(match, TOURNAMENT_TIMEZONE, timezones=timezones, now=now,
                                  include_stage=False, date_as_heading=False))
    return lines


def format_recent_team_matches(matches, timezones, now=None):
    recent = sorted((match for match in matches if match.status == MatchStatus.COMPLETED),
                    key=lambda match: (match.start_time, match.id), reverse=True)[:5]
    if not recent:
        return ["## __Last 5 Matches__", "No finished matches yet. Use Show All Matches to see upcoming games."]
    return ["## __Last 5 Matches__"] + [
        format_match(match, TOURNAMENT_TIMEZONE, timezones=timezones, now=now,
                     include_stage=False, date_as_heading=False) for match in recent
    ]


def format_next_match(match, timezone_name: str, now: datetime, timezones=None) -> str:
    team1 = worlds.teams_by_id[match.team1_id]
    team2 = worlds.teams_by_id[match.team2_id]
    stage = worlds.stages_by_id[match.stage_id]
    stage_name = display_stage_name(stage.name)
    stage_details = stage_name
    if match.round_name and not labels_are_duplicate(stage.name, match.round_name):
        stage_details += f" · {match.round_name}"
    relative = format_relative_time(match.start_time, now)
    official = local_datetime(match.start_time, TOURNAMENT_TIMEZONE)
    date_time_label = f"__{date_heading_token(match.start_time, TOURNAMENT_TIMEZONE, now=now, style='short')}__\n"
    if relative:
        date_time_label += f"\n-# ({relative})"
    date_time_label += "\n" + "\n".join(f"-# {line}" for line in format_timezone_times(match.start_time, timezones or [timezone_name]).splitlines())
    return (
        f"# __Next Worlds Match:__\n"
        f"-# {stage_details} (Bo{match.best_of})\n"
        f"## {matchup(match) if current_policy() else f'{team1.short_name} vs {team2.short_name}'}\n\n"
        f"{date_time_label}"
    )


def default_schedule_stage():
    now = datetime.now(UTC)

    next_match = get_next_match(
        worlds,
        now,
    )

    if next_match:
        return worlds.stages_by_id[next_match.stage_id]

    return max(
        worlds.stages,
        key=lambda stage: stage.order,
    )


async def send_long_message(
    interaction: discord.Interaction,
    header: str,
    lines: list[str],
    *, deferred=False, footer=None, controls=None, max_length=1900, continuation_header=None, paginate=False,
):
    chunks = []
    current = header
    date_heading = None

    for line in lines:
        if line.startswith("## __"):
            date_heading = line
        addition = f"\n\n{line}"

        if len(current) + len(addition) > max_length - (len(footer) + 2 if footer else 0):
            chunks.append(current)

            current = continuation_header or header
            if date_heading and line != date_heading:
                current += f"\n\n{date_heading}"
            current += addition

        else:
            current += addition

    if current:
        chunks.append(current)

    if footer:
        chunks = [chunk + "\n\n" + footer for chunk in chunks]
    if paginate:
        if len(chunks) > 1:
            chunks = [chunk.replace(header, header[:-2] + f" (Page {index}/{len(chunks)})__", 1)
                      if header.endswith("__") else chunk.replace(header, header + f" (Page {index}/{len(chunks)})", 1)
                      for index, chunk in enumerate(chunks, 1)]
        payload = message_payload(chunks[0], controls=HistoryPages(chunks))
        sender = interaction.followup.send if deferred else interaction.response.send_message
        await sender(**payload, allowed_mentions=discord.AllowedMentions.none())
        return
    if deferred:
        await interaction.followup.send(**message_payload(chunks[0], controls=controls if len(chunks) == 1 else None))
    else:
        await interaction.response.send_message(**message_payload(chunks[0], controls=controls if len(chunks) == 1 else None))

    for index, chunk in enumerate(chunks[1:], 1):
        await interaction.followup.send(**message_payload(chunk, controls=controls if index == len(chunks) - 1 else None))


# --------------------------------------------------
# Discord client
# --------------------------------------------------

intents = discord.Intents.default()


class WorldsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)

        self.tree = SetupCommandTree(self)
        self.voting_manager = VotingManager(self, tournament_store, GUILD_ID)
        self.add_dynamic_items(ScheduleVoteButton)
        self.add_dynamic_items(DailyPageButton)
        self.add_dynamic_items(MyStatsButton)
        self.add_dynamic_items(OpenVotePanel, PublicVotersButton)
        self.server_delivery = ServerReminderDelivery(self, tournament_store, GUILD_ID, poll_manager=self.voting_manager)
        self.background_polling = BackgroundPolling(
            tournament_store,
            get_all_user_reminder_preferences,
            self.publish_tournament_data,
            self.wait_until_ready,
            server_delivery=self.server_delivery,
            voting_manager=self.voting_manager,
            pickems_reminder=PickemsReminder(self, tournament_store, GUILD_ID),
        )

    def publish_tournament_data(self, data):
        global worlds
        worlds = data
        if getattr(self, "spoiler_ledger", None):
            self.spoiler_ledger.observe(data)
        guild = self.get_guild(GUILD_ID)
        if guild:
            configure_team_emojis(guild.emojis, data.teams)

    async def setup_hook(self):
        global worlds
        validate_application(self.user.id)
        await tournament_store.refresh()
        worlds = tournament_store.data
        self.spoiler_ledger = SpoilerLedger(get_connection)
        self.spoiler_ledger.observe(worlds)
        self.spoiler_delivery = SpoilerDelivery(self, get_connection)
        self.spoiler_delivery.install()
        self.spoiler_task = asyncio.create_task(self.spoiler_delivery.run(), name="spoiler-release")
        if tournament_store.last_error:
            logger.warning(
                "Using %s tournament data (last successful fetch: %s)",
                tournament_store.source, tournament_store.fetched_at or "never",
            )

        guild = discord.Object(id=GUILD_ID)

        remove_development_commands(self.tree)
        self.tree.copy_global_to(guild=guild)

        synced = await self.tree.sync(guild=guild)

        logger.info(
            "Synced %s command(s) to guild %s",
            len(synced),
            GUILD_ID,
        )
        self.background_polling.start()
        self.player_refresh_task = asyncio.create_task(refresh_players_loop(worlds.tournament.year), name="golgg-players")

    async def close(self):
        spoiler_task = getattr(self, "spoiler_task", None)
        if spoiler_task:
            spoiler_task.cancel()
            await asyncio.gather(spoiler_task, return_exceptions=True)
        if getattr(self, "spoiler_delivery", None):
            self.spoiler_delivery.uninstall()
        task = getattr(self, "player_refresh_task", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.background_polling.stop()
        await super().close()


bot = WorldsBot()


@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    configure_team_emojis(guild.emojis if guild else (), worlds.teams)
    logger.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id,
    )


@bot.event
async def on_guild_emojis_update(guild, before, after):
    if guild.id == GUILD_ID:
        configure_team_emojis(after, worlds.teams)


# --------------------------------------------------
# Setup helpers
# --------------------------------------------------


def build_setup_content(
    user_id: int,
    display_name: str,
    *, member=None, guild=None, all_games_enabled=None,
) -> str:
    safe_display_name = discord.utils.escape_markdown(display_name)

    timezone_name = get_user_timezone(user_id)
    time_format_text = "1:00 am (CEST)" if get_user_time_format(user_id) == "12h" else "1:00h (CEST)"

    followed_team_ids = get_followed_team_ids(user_id)

    (
        _,
        followed_team_server_ping,
        _,
    ) = get_notification_settings(user_id)

    all_games_server_ping = worlds_subscription_enabled(member, guild) if all_games_enabled is None else all_games_enabled

    if timezone_name:
        timezone_text = timezone_name
    else:
        timezone_text = "Not set"

    if followed_team_ids:
        followed_teams = [
            worlds.teams_by_id[team_id]
            for team_id in followed_team_ids
            if team_id in worlds.teams_by_id
        ]

        followed_teams = sorted(
            followed_teams,
            key=lambda team: team.short_name,
        )

        team_text = ", ".join(team.short_name for team in followed_teams)
    else:
        team_text = "None selected"

    if all_games_server_ping:
        all_games_text = "**On** *(Daily posts and 5-minute match reminders)*"
    else:
        all_games_text = "**Off**"
    role = configured_worlds_role(guild)
    if role:
        all_games_text += "\n*(Turn this on to be notified when voting opens.)*"
    else:
        all_games_text += "\nA server manager must select the Worlds role in `/serversetup` first."

    if followed_team_server_ping:
        followed_ping_text = "**On** *(Daily voting posts and 5-minute match reminders)*"
    else:
        followed_ping_text = "**Off**"

    return (
        f"## Worlds Bot Setup for @{safe_display_name}\n\n"
        f"### 🌍 __Timezone:__\n"
        f"{timezone_text}\n\n"
        f"### 🕐 __Time Format:__\n{time_format_text}\n\n"
        f"### ⭐ __Teams You Are Following:__\n"
        f"**{team_text}**\n\n"
        f"### 🔔 __Server Pings for All Games:__\n"
        f"{all_games_text}\n\n"
        f"### 📣 __Server Pings for Followed Teams:__\n"
        f"{followed_ping_text}"
    )


# --------------------------------------------------
# Timezone picker
# --------------------------------------------------


class TimezoneRegionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=region,
                value=region,
            )
            for region in TIMEZONE_REGIONS
        ]

        super().__init__(
            placeholder=("Choose your region..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        region = self.values[0]

        await interaction.response.edit_message(
            **message_payload("## Choose Your Timezone\n\n"
                f"Region: **{region}**\n\n"
                "Choose the closest city.", controls=TimezoneCityView(region)),

        )


class TimezoneRegionView(discord.ui.View):
    setup_controls = True
    def __init__(self):
        super().__init__(timeout=900)

        self.add_item(TimezoneRegionSelect())


class TimezoneCitySelect(discord.ui.Select):
    def __init__(
        self,
        region: str,
    ):
        self.region = region

        options = [
            discord.SelectOption(
                label=label,
                value=timezone_name,
            )
            for (
                label,
                timezone_name,
            ) in TIMEZONE_REGIONS[region]
        ]

        super().__init__(
            placeholder=("Choose the closest city..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        timezone_name = self.values[0]

        set_user_timezone(
            interaction.user.id,
            timezone_name,
        )

        await interaction.response.edit_message(
            **setup_payload(interaction),

        )


class TimezoneCityView(discord.ui.View):
    setup_controls = True
    def __init__(
        self,
        region: str,
    ):
        super().__init__(timeout=900)

        self.add_item(TimezoneCitySelect(region))


# --------------------------------------------------
# Team picker
# --------------------------------------------------


class TeamSelect(discord.ui.Select):
    def __init__(
        self,
        user_id: int,
    ):
        self.user_id = user_id
        followed_team_ids = get_followed_team_ids(user_id)

        teams = sorted(
            [team for team in worlds.teams if team.id != TBD_TEAM_ID],
            key=lambda team: (team.short_name),
        )

        options = [
            discord.SelectOption(
                label=team.name,
                emoji=team_emoji(team.id),
                description=(team.short_name),
                value=team.id,
                default=(team.id in followed_team_ids),
            )
            for team in teams
        ]

        super().__init__(
            placeholder=("Choose Teams to Follow..."),
            min_values=0,
            max_values=len(options),
            options=options,
            row=2,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/mysetup` to change your own settings."), ephemeral=True)
            return
        selected_teams = set(self.values)

        set_followed_team_ids(
            interaction.user.id,
            selected_teams,
        )

        await interaction.response.edit_message(
            **setup_payload(interaction),

        )


# --------------------------------------------------
# Main setup view
# --------------------------------------------------


class SetupTimezoneSelect(discord.ui.Select):
    def __init__(self, user_id, region=None):
        self.user_id = user_id
        self.region = region
        if region:
            current = get_user_timezone(user_id)
            options = [discord.SelectOption(label=city, value=zone, default=zone == current)
                       for city, zone in TIMEZONE_REGIONS[region]]
            options.append(discord.SelectOption(label="Choose Another Region", value="__region__"))
        else:
            options = [discord.SelectOption(label=name, value=name) for name in TIMEZONE_REGIONS]
        super().__init__(placeholder="Choose your timezone city..." if region else "Choose your timezone region...",
                         options=options, row=0)

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/mysetup` to change your own settings."), ephemeral=True)
            return
        value = self.values[0]
        region = None
        if not self.region:
            region = value
        elif value != "__region__":
            set_user_timezone(self.user_id, value)
        await interaction.response.edit_message(**setup_payload(interaction, timezone_region=region))


class TimeFormatSelect(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = user_id
        current = get_user_time_format(user_id)
        super().__init__(placeholder="Choose your time format", row=1, options=[
            discord.SelectOption(label="24-hour: 1:00h (CEST)", value="24h", default=current == "24h"),
            discord.SelectOption(label="12-hour: 1:00 am (CEST)", value="12h", default=current == "12h"),
        ])

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/mysetup` to change your own settings."), ephemeral=True)
            return
        set_user_time_format(interaction.user.id, self.values[0])
        await interaction.response.edit_message(**setup_payload(interaction))


class SetupView(discord.ui.View):
    setup_controls = True
    control_after_paragraphs = (2, 3, 4, 5, 6)

    def __init__(
        self,
        user_id: int,
        timezone_region=None,
        *, member=None, guild=None, all_games_enabled=None,
    ):
        super().__init__(timeout=600)

        self.user_id = user_id
        self.add_item(CitySearchButton(user_id, setup_payload))
        self.add_item(TimeFormatSelect(user_id))
        self.add_item(TeamSelect(user_id))

        (
            _,
            followed_team_server_ping,
            _,
        ) = get_notification_settings(user_id)

        all_games_server_ping = worlds_subscription_enabled(member, guild) if all_games_enabled is None else all_games_enabled
        self.toggle_all_games_ping.disabled = configured_worlds_role(guild) is None

        if all_games_server_ping:
            self.toggle_all_games_ping.label = "Turn Server Pings for All Games OFF (Currently ON)"

            self.toggle_all_games_ping.style = discord.ButtonStyle.success
        else:
            self.toggle_all_games_ping.label = "Turn Server Pings for All Games ON (Currently OFF)"

            self.toggle_all_games_ping.style = discord.ButtonStyle.secondary

        if followed_team_server_ping:
            self.toggle_server_ping.label = "Turn Server Pings for Your Teams OFF (Currently ON)"

            self.toggle_server_ping.style = discord.ButtonStyle.success
        else:
            self.toggle_server_ping.label = "Turn Server Pings for Your Teams ON (Currently OFF)"

            self.toggle_server_ping.style = discord.ButtonStyle.secondary

    @discord.ui.button(
        label="All-Game Pings",
        style=discord.ButtonStyle.secondary,
        emoji="🔔",
        row=3,
    )
    async def toggle_all_games_ping(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/mysetup` to change your own settings."), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            enabled = await toggle_worlds_subscription(interaction)
        except ValueError as error:
            await interaction.followup.send(**message_payload(str(error)), ephemeral=True)
            return
        await interaction.edit_original_response(**setup_payload(interaction, all_games_enabled=enabled))

    @discord.ui.button(
        label="Followed-Team Pings",
        style=discord.ButtonStyle.secondary,
        emoji="📣",
        row=4,
    )
    async def toggle_server_ping(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        (
            _,
            server_ping,
            _,
        ) = get_notification_settings(interaction.user.id)

        set_server_ping(
            interaction.user.id,
            not server_ping,
        )

        await interaction.response.edit_message(
            **setup_payload(interaction),

        )

# --------------------------------------------------
# User requirements
# --------------------------------------------------


def setup_payload(interaction, *, timezone_region=None, all_games_enabled=None):
    context = dict(member=interaction.user, guild=getattr(interaction, "guild", None),
                   all_games_enabled=all_games_enabled)
    payload = message_payload(build_setup_content(interaction.user.id, interaction.user.display_name, **context),
                              controls=SetupView(interaction.user.id, timezone_region=timezone_region, **context))
    payload["view"].expire_with(interaction)
    return payload


def read_only_setup_payload(member, guild):
    context = dict(member=member, guild=guild)
    payload = message_payload(build_setup_content(member.id, member.display_name, **context),
                              controls=SetupView(member.id, **context))
    # Build with the same paragraph boundaries and spacing as /mysetup, then
    # remove every interactive row rather than merely disabling the controls.
    view = payload["view"]
    for item in list(view.walk_children()):
        if isinstance(item, discord.ui.ActionRow):
            item.parent.remove_item(item)
    view.controls = None
    return payload


async def require_timezone(
    interaction: discord.Interaction,
) -> str | None:
    timezone_name = get_user_timezone(interaction.user.id)

    if timezone_name:
        return timezone_name

    await interaction.response.send_message(
        **message_payload("## Set Your Timezone First\n\n"
        "Run `/mysetup` to set your timezone, or choose your region below. "
        "The bot will use your timezone "
        "for all match times.", controls=TimezoneRegionView()),

        ephemeral=True,
    )

    return None


# --------------------------------------------------
# Autocomplete
# --------------------------------------------------


async def team_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    from services.spoilers import padded_name, current_policy
    current = current.lower()

    matches = [
        team
        for team in worlds.teams
        if team.id != TBD_TEAM_ID
        if (current in team.name.lower() or current in team.short_name.lower())
    ]

    return [
        app_commands.Choice(
            name=(padded_name(team.short_name, current_policy().width) if current_policy()
                  else f"{team.name} ({team.short_name})"),
            value=team.id,
        )
        for team in matches[:25]
    ]


async def player_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=name, value=player_id)
            for player_id, name in player_catalog(worlds.tournament.year, current)[:25]]


async def stage_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    current = current.lower()

    matches = [stage for stage in worlds.stages if (current in stage.name.lower())]

    return [
        app_commands.Choice(
            name=stage.name,
            value=stage.id,
        )
        for stage in matches[:25]
    ]


# --------------------------------------------------
# Commands
# --------------------------------------------------


@bot.tree.command(
    name="ping",
    description=("Check whether the Worlds bot is online."),
)
async def ping(
    interaction: discord.Interaction,
):
    latency_ms = round(bot.latency * 1000)

    await interaction.response.send_message(**message_payload(f"🏓 Pong! `{latency_ms} ms`"))


# Temporary developer command
@bot.tree.command(
    name="test-reminder",
    description="Preview reminder recipients for a sample matchup, without sending any pings.",
)
async def test_reminder(
    interaction: discord.Interaction,
):
    users = get_all_user_reminder_preferences()

    ping_user_ids = get_server_ping_user_ids(
        users,
        "T1",
        "G2",
    )

    if ping_user_ids:
        user_list = "\n".join(f"- `{user_id}`" for user_id in sorted(ping_user_ids))
    else:
        user_list = "Nobody"

    message = (
        "## Reminder Target Test\n\n"
        "**Match:** T1 vs G2\n\n"
        "**Users who would be server-pinged:**\n"
        f"{user_list}"
    )

    await interaction.response.send_message(
        **message_payload(message),
        ephemeral=True,
    )


# Temporary developer command
@bot.tree.command(
    name="simulate-reminder",
    description="Preview a five-minute reminder for the next scheduled match, without sending any pings.",
)
async def simulate_reminder(
    interaction: discord.Interaction,
):
    users = get_all_user_reminder_preferences()

    scheduled_matches = sorted(
        [
            match
            for match in worlds.matches
            if match.status == MatchStatus.SCHEDULED
        ],
        key=lambda match: match.start_time,
    )

    if not scheduled_matches:
        await interaction.response.send_message(
            **message_payload("No scheduled matches found in the current tournament data."),
            ephemeral=True,
        )
        return

    match = scheduled_matches[0]

    simulated_now = match.start_time - timedelta(minutes=5)

    tracker = ReminderTracker()

    result = check_reminders(
        matches=[match],
        users=users,
        now=simulated_now,
        tracker=tracker,
    )

    if not result.server_reminders:
        await interaction.response.send_message(
            **message_payload("No server reminder was triggered."),
            ephemeral=True,
        )
        return

    reminder = result.server_reminders[0]

    team1 = worlds.teams_by_id[reminder.team1_id]

    team2 = worlds.teams_by_id[reminder.team2_id]

    if reminder.ping_user_ids:
        mentions = " ".join(
            f"<@{user_id}>" for user_id in sorted(reminder.ping_user_ids)
        )
    else:
        mentions = "*Nobody would be pinged.*"

    date, time = private_match_details(simulated_now, get_user_timezone(interaction.user.id),
                                      time_format=get_user_time_format(interaction.user.id))
    message = (
        "## 🧪 Simulated Server Reminder\n\n"
        f"**{team1.short_name}** vs "
        f"**{team2.short_name}** starts "
        f"in 5 minutes!\n\n"
        f"{mentions}\n\n"
        f"-# Simulated time: "
        f"{date} · {time}"
    )

    await interaction.response.send_message(
        **message_payload(message),
        ephemeral=True,
    )


@bot.tree.command(name="serversetup", description="Configure the Worlds ping role, reminder channel, and settings. Requires Manage Server.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def server_setup(interaction: discord.Interaction):
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message(**message_payload("This bot is configured for a different server."), ephemeral=True)
        return
    await interaction.response.send_message(
        **message_payload(build_server_setup_content(interaction.guild_id), controls=ServerSetupView(interaction.guild_id)),
         ephemeral=True,
    )


@bot.tree.command(
    name="mysetup",
    description=("Choose your timezone, time format, teams, and Worlds role and match reminder preferences."),
)
async def setup(
    interaction: discord.Interaction,
):
    await interaction.response.defer(ephemeral=True)
    try:
        enabled = await initialize_worlds_subscription(interaction)
    except (ValueError, discord.HTTPException) as error:
        await interaction.followup.send(**message_payload(str(error)), ephemeral=True)
        enabled = None
    await interaction.edit_original_response(
        **setup_payload(interaction, all_games_enabled=enabled),

    )
    if interaction.guild_id is not None:
        await asyncio.to_thread(mark_setup_completed, interaction.guild_id, interaction.user.id)


@bot.tree.command(name="setup", description="Privately inspect a member's settings without changing them. Requires Manage Server.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Choose the server member whose settings you want to inspect.")
async def inspect_setup(interaction: discord.Interaction, name: discord.Member):
    if not can_manage_server(interaction, GUILD_ID):
        await interaction.response.send_message(**message_payload(
            "You need the Manage Server permission in this bot's server to inspect member settings."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        # Fetch current server membership/roles rather than trusting a cached
        # role snapshot when reporting the all-games notification subscription.
        member = await interaction.guild.fetch_member(name.id)
    except discord.NotFound:
        await interaction.followup.send(**message_payload("That member is no longer in this server."), ephemeral=True)
        return
    payload = read_only_setup_payload(member, interaction.guild)
    payload["view"].expire_with(interaction)
    await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="nextgames", description="Show upcoming Worlds matches (default: 3).")
@app_commands.describe(count="Number of matches to show, from 1 to 100 (default: 3).")
async def next_match_command(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 3):
    await show_match_games(interaction, worlds, format_match, count=count, upcoming=True, now=datetime.now(UTC))


@bot.tree.command(name="previousgames", description="Show completed Worlds matches (default: 3), with spoiler protection.")
@app_commands.describe(count="Number of matches to show, from 1 to 100 (default: 3).")
async def previous(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 3):
    await show_match_games(interaction, worlds, format_match, count=count, upcoming=False, now=datetime.now(UTC))


async def show_day_schedule(interaction: discord.Interaction, day_offset: int):
    timezone_name = TOURNAMENT_TIMEZONE
    now = datetime.now(UTC)
    target_date = now.astimezone(ZoneInfo(timezone_name)).date() + timedelta(days=day_offset)
    matches = get_matches_for_local_date(worlds, target_date, timezone_name)
    timezones = get_registered_timezones() or [timezone_name]
    label = "Today" if day_offset == 0 else "Tomorrow"
    title = f"__{worlds.tournament.year} Worlds: {label}__"
    if not matches:
        await interaction.response.send_message(
            **message_payload(f"## {title}\n\nNo games {label.lower()}."),
            ephemeral=False, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    date_label = date_heading_token(target_date, timezone_name, now=now, style="short")
    context = f"__{date_label}__\n\n"
    manager = getattr(getattr(interaction, "client", None), "voting_manager", None)
    deferred = False
    if day_offset == 0 and manager and getattr(interaction, "guild_id", None) == manager.guild_id:
        await interaction.response.defer(ephemeral=False)
        deferred = True
        context += await daily_post_reference(interaction.guild_id, worlds.tournament.id, target_date.isoformat()) + "\n\n"
    if day_offset == 0 and matches and manager and getattr(interaction, "guild_id", None) == manager.guild_id:
        poll_by_id = await manager.prepare_schedule_polls(matches)
        entries = [(match, format_match(match, timezone_name, include_date=False, now=now, timezones=timezones))
                   for match in matches]
        for payload in schedule_vote_payloads(manager, title, context, entries, poll_by_id, personal_panel=True):
            await interaction.followup.send(**payload, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
            await manager.acknowledge_schedule_opportunities(payload["view"].matches, poll_by_id)
        return
    descriptions = []
    current = context
    help_size = len(VOTE_CONFIRMATION_HELP) + 2 if day_offset == 0 else 0
    for match in matches:
        text = format_match(match, timezone_name, include_date=False, now=now, timezones=timezones)
        if len(current) + len(text) + 2 + help_size > 3800 and current != context:
            descriptions.append(current)
            current = context
        current += text + "\n\n"
    descriptions.append(current)
    for index, description in enumerate(descriptions):
        if day_offset == 0 and matches:
            description = description.rstrip() + "\n\n" + VOTE_CONFIRMATION_HELP
        embed = discord.Embed(title=title, description=description.rstrip(), colour=PREDICTION_BLUE)
        if index == 0 and not deferred:
            await interaction.response.send_message(**embed_card_payload(embed), ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.followup.send(**embed_card_payload(embed), ephemeral=False, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="today", description="Show today's matches, link the daily voting post, and open your private voting panel.")
async def today(interaction: discord.Interaction):
    await show_day_schedule(interaction, 0)


@bot.tree.command(name="tomorrow", description="Show tomorrow's Worlds matches in the server's registered timezones.")
async def tomorrow(interaction: discord.Interaction):
    await show_day_schedule(interaction, 1)


@bot.tree.command(
    name="schedule",
    description=("Browse the next five Worlds matches or the schedule for a tournament stage."),
)
@app_commands.describe(
    view="Choose the next five games or a full tournament stage",
)
@app_commands.choices(view=[
    app_commands.Choice(name="Next 5 Games", value="next-five"),
    *[
        app_commands.Choice(name=display_stage_name(stage.name), value=stage.id)
        for stage in sorted(worlds.stages, key=lambda stage: stage.order)
    ],
])
async def schedule(
    interaction: discord.Interaction,
    view: str,
):
    timezone_name = TOURNAMENT_TIMEZONE
    now = datetime.now(UTC)
    if view == "next-five":
        matches = sorted(
            [match for match in worlds.matches
             if match.status == MatchStatus.SCHEDULED and match.start_time > now],
            key=lambda match: match.start_time,
        )[:5]
        view_title = "Next 5 Games"
    else:
        selected_stage = worlds.stages_by_id.get(view)

        if selected_stage is None:
            await interaction.response.send_message(
                **message_payload("I couldn't find that schedule view."),
                ephemeral=True,
            )

            return

        matches = sorted(
            get_matches_for_stage(worlds, selected_stage.id),
            key=lambda match: match.start_time,
        )
        stage_name = display_stage_name(selected_stage.name)
        view_title = {
            "Play-ins": "Play-Ins Stage",
            "Swiss Stage": "Swiss Stage",
            "Quarterfinals": "Quarterfinals Stage",
            "Semifinals": "Semifinals Stage",
            "Final": "Finals Stage",
        }.get(stage_name, stage_name)

    if not matches:
        await interaction.response.send_message(
            **message_payload("No upcoming scheduled matches found." if view == "next-five"
            else f"No matches found for {display_stage_name(selected_stage.name)}.")
        )

        return

    header = f"# __{worlds.tournament.year} Worlds Schedule: {view_title}__"

    lines = []
    current_date = None
    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]

    for match in matches:
        local_match_time = local_datetime(
            match.start_time,
            TOURNAMENT_TIMEZONE,
        )

        match_date = local_match_time.date()

        if match_date != current_date:
            current_date = match_date

            local_date = format_local_date(
                match.start_time,
                TOURNAMENT_TIMEZONE,
            )

            lines.append(f"## __{date_heading_token(match.start_time, TOURNAMENT_TIMEZONE, now=now)}__")

        lines.append(
            format_match(
                match,
                timezone_name,
                include_date=False,
                now=now,
                timezones=timezones,
            )
        )

    await send_long_message(
        interaction,
        header,
        lines,
        paginate=True,
    )


@bot.tree.command(
    name="team",
    description=("Show a team's Worlds matches and gol.gg tournament stats."),
)
@app_commands.describe(
    team="Choose a Worlds team",
)
@app_commands.autocomplete(team=team_autocomplete)
async def team(
    interaction: discord.Interaction,
    team: str,
):
    timezone_name = TOURNAMENT_TIMEZONE
    selected_team = worlds.teams_by_id.get(team.upper())

    if selected_team is None:
        await interaction.response.send_message(
            **message_payload("I couldn't find that team."),
            ephemeral=True,
        )

        return

    await interaction.response.defer()
    stats = await team_stats_text(selected_team, worlds.tournament.year)
    matches = get_matches_for_team(
        worlds,
        selected_team.id,
    )

    matches = sorted(
        matches,
        key=lambda match: (match.start_time),
    )

    header = f"## {selected_team.name}\n" f"**Region:** " f"{selected_team.region}"
    header += "\n\n" + stats

    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]
    lines = format_recent_team_matches(matches, timezones) if matches else ["No confirmed matchups are available for this team yet. They will appear when the live schedule names its opponents."]
    controls = TeamHistoryView(selected_team.name,
        lambda: format_team_matches(get_matches_for_team(worlds, selected_team.id), timezones), has_matches=bool(matches))

    await send_long_message(
        interaction,
        header,
        lines,
        deferred=True,
        footer=GOLGG_FOOTNOTES,
        controls=controls,
        max_length=3700,
        continuation_header=f"## {selected_team.name} · Matches",
    )


@bot.tree.command(name="player", description="View a player's Worlds stats from gol.gg. Start typing to find a player.")
@app_commands.describe(name="Choose a player, or start typing their name")
@app_commands.autocomplete(name=player_autocomplete)
async def player(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    text = await player_reply(name, worlds.tournament.year)
    await interaction.followup.send(**message_payload(text),
                                    allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="testteam", description="Privately preview a team's sample finished, live, and upcoming matches.")
@app_commands.describe(team="Choose a team for the sample preview")
@app_commands.autocomplete(team=team_autocomplete)
async def testteam(interaction: discord.Interaction, team: str):
    selected = worlds.teams_by_id.get(team.upper())
    opponents = [item for item in worlds.teams if item.id not in {team.upper(), "TBD"}][:3]
    if selected is None or selected.id == "TBD" or not opponents:
        await interaction.response.send_message(**message_payload("Choose a known Worlds team."), ephemeral=True)
        return
    stage = next((item for item in worlds.stages if "swiss" in item.name.lower()), worlds.stages[0])
    now = datetime.now(UTC)
    samples = []
    for index, status in enumerate((MatchStatus.COMPLETED, MatchStatus.LIVE, MatchStatus.SCHEDULED)):
        samples.append(Match(
            id=f"sample-team-{index}", tournament_id=worlds.tournament.id, stage_id=stage.id,
            team1_id=selected.id, team2_id=opponents[index % len(opponents)].id,
            start_time=now + timedelta(hours=(-24, -1, 24)[index]), best_of=3, status=status,
            team1_score=2 if index == 0 else 1 if index == 1 else None,
            team2_score=1 if index < 2 else None, winner_id=selected.id if index == 0 else None,
        ))
    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]
    text = (f"## TEST: {selected.name}\n**Region:** {selected.region or 'Unknown'}\n\n"
            "Sample matches and results only. The real schedule is unchanged.\n\n"
            + "\n\n".join(format_team_matches(samples, timezones, now=now)))
    await interaction.response.send_message(**message_payload(text), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(
    name="swiss",
    description=("Open the official Worlds Swiss bracket and standings."),
)
async def standings(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(**message_payload(
        "## Worlds Swiss Bracket\n\n"
        "[Open the official Swiss bracket and standings](https://lolesports.com/en-GB/tournament/115660540725177488/stage/115660540725570706)"),
        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="teststandings", description="Privately preview sample Swiss standings without changing live data.")
async def teststandings(interaction: discord.Interaction):
    teams = sorted((team for team in worlds.teams if team.id != "TBD"), key=lambda team: team.short_name)[:16]
    figures = [(2, 0)] * 4 + [(1, 1)] * 8 + [(0, 2)] * 4
    records = [(team.id, wins, losses) for team, (wins, losses) in zip(teams, figures)]
    await interaction.response.send_message(**message_payload(standings_text(worlds, records, preview=True)),
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="pickems", description="Open the server's official Riot Pick'ems leaderboard.")
@app_commands.guild_only()
async def pickems(interaction: discord.Interaction):
    settings = get_server_settings(interaction.guild_id)
    if settings.pickems_url:
        await interaction.response.send_message(
            **message_payload(f"## __Pick'ems Leaderboard__\n\n[Go to our official Pick'ems leaderboard](<{settings.pickems_url}>)"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.send_message(**message_payload("The Pick'ems link hasn't been set yet. A server admin can add it in `/serversetup`."), ephemeral=True)


@bot.tree.command(name="vote", description="Vote or change your predictions privately; your selected teams have a green checkmark.")
@app_commands.guild_only()
async def changevote(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    manager = (getattr(interaction, "extras", {}).get("vote_panel_manager")
               or getattr(interaction.client, "prediction_test_manager", None) or interaction.client.voting_manager)
    try:
        available = await manager.personal_vote_targets(interaction.user.id)
    except ValueError as error:
        await interaction.followup.send(**message_payload(str(error)), ephemeral=True)
        return
    if not available:
        await interaction.followup.send(**message_payload("## __Your Currently Active Predictions__\n\nVoting is closed for now. Use `/today` or `/tomorrow` to see the schedule."), ephemeral=True)
        return
    poll = available[0]
    saved = await asyncio.to_thread(get_user_votes, poll.guild_id, poll.tournament_id, interaction.user.id)
    timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
    time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
    for payload in personal_vote_payloads(manager, available, interaction.user.id, saved,
                                         timezone_name=timezone_name, time_format=time_format):
        await interaction.followup.send(**payload,
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="dailyvote", description="Find the latest daily Worlds voting post in this server.")
@app_commands.guild_only()
async def dailyvote(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    post = await asyncio.to_thread(get_latest_daily_post, interaction.guild_id, worlds.tournament.id)
    if post is None:
        text = "## __Daily Voting Post__\n\nNo daily voting post has been published in this server yet. Use `/today` to see today's matches."
    else:
        day, channel_id, message_id = post
        link = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"
        text = f"## __Latest Daily Voting Post__\n\nMatchday: {day}\n\n[Open the daily voting post]({link})\n\n-# Voting closes 5 minutes before each match."
    await interaction.followup.send(**message_payload(text), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="currentvotes", description="View current match voting; everyone's picks appear after voting closes.")
@app_commands.guild_only()
async def currentvotes(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.now(UTC)
    data = worlds
    recent = await asyncio.to_thread(get_recent_completed_match_ids, interaction.guild_id, data.tournament.id, now)
    matches = get_matches_for_votes(data, now, recent)
    if not matches:
        await interaction.followup.send(**message_payload("No upcoming, live, or recently completed matches found."), allowed_mentions=discord.AllowedMentions.none())
        return
    manager = getattr(getattr(interaction, "client", None), "voting_manager", None)
    available = {}
    if manager:
        try:
            available = {poll.match_id: poll for poll in await manager.personal_vote_targets(interaction.user.id)}
        except ValueError:
            pass
    cards = []
    for match in matches:
        stage = data.stages_by_id.get(match.stage_id)
        try:
            rows = await asyncio.to_thread(get_upcoming_match_votes, interaction.guild_id, match.id)
        except ValueError:
            voting_closed = match.status != MatchStatus.SCHEDULED or now >= match.start_time - timedelta(minutes=5)
            no_votes = voting_closed and not await asyncio.to_thread(has_match_votes, interaction.guild_id, match.id)
            controls = current_votes_controls(manager, available[match.id]) if match.id in available else None
            cards.append(hidden_votes_payload(match, now=now, stage_name=stage.name if stage else None, no_votes=no_votes,
                                              controls=controls))
        else:
            cards.append({"embed": upcoming_votes_embed(match, rows, stage_name=stage.name if stage else None)})
    for payload in group_voting_messages(cards):
        await interaction.followup.send(**payload, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="votehistory", description="See the latest series' voting results, or a member's prediction history.")
@app_commands.guild_only()
@app_commands.describe(name="Choose a server member who has voted before.")
@app_commands.autocomplete(name=voter_autocomplete)
async def votehistory(interaction: discord.Interaction, name: str | None = None):
    if name is None:
        from services.series_results import show_latest_results
        await show_latest_results(interaction)
    else:
        await show_vote_history(interaction, name)


@bot.tree.command(name="myvotes", description="View your past and upcoming predictions, check results, and change open picks.")
@app_commands.guild_only()
async def myvotes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    test_manager = getattr(getattr(interaction, "client", None), "prediction_test_manager", None)
    manager = test_manager or getattr(getattr(interaction, "client", None), "voting_manager", None)
    available = []
    if manager:
        try:
            available = await manager.personal_vote_targets(interaction.user.id)
        except ValueError:
            pass
    data = test_manager.store.data if test_manager else worlds
    now = manager.clock() if manager else None
    query_options = {"include_open": True}
    if test_manager:
        query_options["now"] = now
    rows = await asyncio.to_thread(get_user_votes, interaction.guild_id, data.tournament.id, interaction.user.id, **query_options)
    timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
    time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
    if manager:
        for payload in my_vote_payloads(manager, rows, available, interaction.user.id, data, now=now,
                                        test_mode=bool(test_manager), display_name=interaction.user.display_name,
                                        timezone_name=timezone_name, time_format=time_format, guild=getattr(interaction, "guild", None)):
            await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    for embed in my_votes_embeds(rows, data, display_name=interaction.user.display_name,
                                timezone_name=timezone_name, time_format=time_format, guild=getattr(interaction, "guild", None), now=now):
        if test_manager:
            embed.title = "__TEST: " + embed.title.removeprefix("__")
        await interaction.followup.send(**embed_card_payload(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="See Communism's Worlds prediction standings.")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    _, entries, previous = await leaderboard_context(interaction, worlds.tournament.id, data=worlds)
    payload = leaderboard_payload(entries, interaction.guild_id, worlds.tournament.id, previous=previous)
    await interaction.followup.send(**payload, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="myvotestats", description="View your rank, accuracy, streaks, and advanced personal prediction stats.")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    await show_personal_stats(interaction, interaction.guild_id, worlds.tournament.id, data=worlds)


@bot.tree.command(
    name="help",
    description=("List every bot command and explain what it does."),
)
@app_commands.describe(category="Choose a help section, or leave blank to see all commands.")
@app_commands.choices(category=[
    app_commands.Choice(name="Setup", value=1),
    app_commands.Choice(name="Voting", value=2),
    app_commands.Choice(name="Leaderboards", value=3),
    app_commands.Choice(name="Match and Player Infos", value=4),
])
async def help_command(
    interaction: discord.Interaction,
    category: int | None = None,
):
    message = """# Worlds Bot: List of all Commands

## __1. Setup__

`/mysetup`
Choose your timezone, time format, teams, and Worlds role and match reminder preferences.

## __2. Voting__

`/vote`
Open your private voting panel. Vote or change a pick; your selected team is marked green with ✓.
Voter names appear publicly under each daily matchup, without pings. Picks stay private until voting closes.

`/votehistory` — Post the latest completed series’ voting results, even if the automatic post is missing. No pings.
`/votehistory [name]` (e.g. `/votehistory @apodso`)
Choose a server member from the suggestions to view their past predictions and stats. Choose yourself for your own history and the My Stats button.

## __3. Leaderboards__

`/leaderboard`
See Communism’s leaderboard for predictions made through this bot.

`/pickems`
Open the server’s leaderboard for Riot’s official Pick’ems.

## __4. Match and Player Infos__

`/today`
See today’s matches and a link to today’s daily voting post. Open your private panel to vote or change a pick.

`/tomorrow`
See tomorrow’s matches in the server’s registered timezones.

`/nextgames [count]` (e.g. `/nextgames 6`)
See the next 3 matches by default, or choose 1–100. Browse 3 matches per page.

`/previousgames [count]` (e.g. `/previousgames 6`)
See the last 3 completed matches by default, or choose 1–100, with timed spoiler protection. Browse 3 matches per page.

`/schedule <view>` (e.g. `/schedule Swiss Stage`)
Browse the next five matches or view the schedule for a tournament stage.

`/team <teamname>` (e.g. `/team T1`)
See a specific team’s Worlds matches and tournament stats from gol.gg.

`/player <playername>` (e.g. `/player Zeka`)
See a player’s Worlds stats from gol.gg. Start typing to find a player.

`/swiss`
Open the official visual Swiss bracket and standings on LoL Esports."""
    if category is not None:
        sections = message.split("\n\n## ")
        message = sections[0] + "\n\n## " + sections[category]
    await interaction.response.send_message(**message_payload(message, title_level=1), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


# --------------------------------------------------
# Error handling
# --------------------------------------------------


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    logger.exception(
        "Slash command error: %s",
        error,
    )

    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    else:
        message = "Something went wrong while " "running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                **message_payload(message),
                ephemeral=True,
            )

        else:
            await interaction.response.send_message(
                **message_payload(message),
                ephemeral=True,
            )

    except (
        discord.NotFound,
        discord.HTTPException,
    ):
        logger.warning("Could not send error message " "for expired interaction.")


# --------------------------------------------------
# Start bot
# --------------------------------------------------

for retired_command in ("changevote", "myvotes", "currentvotes", "dailyvote", "myvotestats"):
    bot.tree.remove_command(retired_command)

bot.run(
    TOKEN,
    log_handler=None,
)
