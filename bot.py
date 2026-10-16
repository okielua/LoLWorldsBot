import logging
import asyncio
import os
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from services.presentation import embed_card_payload, message_payload
from discord import app_commands
from dotenv import load_dotenv

from models import MatchStatus
from services.delivery_records import initialize_delivery_records
from services.background_polling import BackgroundPolling
from services.live_tournament import LiveTournamentStore, TBD_TEAM_ID
from services.lolesports import LoLEsportsClient, QUERY_HASH
from services.server_reminders import ServerReminderDelivery
from services.server_settings import initialize_server_settings
from services.server_setup import ServerSetupView, build_server_setup_content
from services.polls import get_user_votes, get_upcoming_match_votes, get_recent_completed_match_ids, has_match_votes, initialize_polls
from services.prediction_display import PREDICTION_BLUE, change_vote_text, group_voting_messages, hidden_votes_payload, my_votes_embeds, upcoming_votes_embed
from services.match_times import TOURNAMENT_TIMEZONE, format_clock_time, format_timezone_times, private_match_details
from services.user_settings import get_registered_timezones
from services.server_settings import get_server_settings
from services.voting import PersonalVoteView, VotingManager
from services.prediction_stats import get_competition_stats
from services.status_labels import MATCH_STATUS_LABELS
from services.reminder_scheduler import check_reminders
from services.reminder_engine import (
    ReminderTracker,
    get_server_ping_user_ids,
)
from services.timezones import TIMEZONE_REGIONS
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
    get_all_games_server_ping,
    get_all_user_reminder_preferences,
    get_followed_team_ids,
    get_notification_settings,
    get_user_timezone,
    get_user_time_format,
    initialize_database,
    set_all_games_server_ping,
    set_followed_team_ids,
    set_server_ping,
    set_user_timezone,
    set_user_time_format,
)

# --------------------------------------------------
# Configuration
# --------------------------------------------------

load_dotenv()

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
    load_tournament_data(DATA_FILE),
    BASE_DIR / "data" / "worlds_2026_cache.json",
    LoLEsportsClient(
        client_version=os.getenv("LOLESPORTS_CLIENT_VERSION", "4078aa9"),
        query_hash=os.getenv("LOLESPORTS_QUERY_HASH", QUERY_HASH),
    ),
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

    if include_date:
        parts.append(f"## __{local_date}__")

    stage_details = [display_stage_name(stage.name)]

    if match.round_name and not labels_are_duplicate(
        stage.name,
        match.round_name,
    ):
        stage_details.append(match.round_name)

    stage_details.append(f"Bo{match.best_of}")

    parts.append(('-# ' if small_details else '') + ' · '.join(stage_details))
    times = format_timezone_times(match.start_time, timezones or [timezone_name])
    if small_details:
        times = "\n".join(f"-# {line}" for line in times.splitlines())
    countdown = f"-# {relative_time.strip()}\n" if relative_time else ""
    parts.append(f"## {team1.short_name} vs {team2.short_name}\n{countdown}{times}")

    if match.status == MatchStatus.COMPLETED:
        if match.team1_score is not None and match.team2_score is not None:
            result = f"{team1.short_name} {match.team1_score}–{match.team2_score} {team2.short_name}"
        elif match.winner_id:
            result = f"Winner: {worlds.teams_by_id[match.winner_id].short_name}"
        else:
            result = "Result unavailable."
        if hide_result:
            result = f"||{result}||"
        parts.append(f"{MATCH_STATUS_LABELS[match.status]}\n### Result: {result}")

    elif match.status.value == "live":
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
    date_time_label = f"__{official.strftime('%a')}, {official.day} {official.strftime('%b %Y')}__\n"
    if relative:
        date_time_label += f"\n-# ({relative})"
    date_time_label += "\n" + "\n".join(f"-# {line}" for line in format_timezone_times(match.start_time, timezones or [timezone_name]).splitlines())
    return (
        f"# __Next Worlds Match:__\n"
        f"-# {stage_details} (Bo{match.best_of})\n"
        f"## {team1.short_name} vs {team2.short_name}\n\n"
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
):
    chunks = []
    current = header
    date_heading = None

    for line in lines:
        if line.startswith("## __"):
            date_heading = line
        addition = f"\n\n{line}"

        if len(current) + len(addition) > 1900:
            chunks.append(current)

            current = header
            if date_heading and line != date_heading:
                current += f"\n\n{date_heading}"
            current += addition

        else:
            current += addition

    if current:
        chunks.append(current)

    await interaction.response.send_message(**message_payload(chunks[0]))

    for chunk in chunks[1:]:
        await interaction.followup.send(**message_payload(chunk))


# --------------------------------------------------
# Discord client
# --------------------------------------------------

intents = discord.Intents.default()


class WorldsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.voting_manager = VotingManager(self, tournament_store, GUILD_ID)
        self.server_delivery = ServerReminderDelivery(self, tournament_store, GUILD_ID, poll_manager=self.voting_manager)
        self.background_polling = BackgroundPolling(
            tournament_store,
            get_all_user_reminder_preferences,
            self.publish_tournament_data,
            self.wait_until_ready,
            server_delivery=self.server_delivery,
            voting_manager=self.voting_manager,
        )

    def publish_tournament_data(self, data):
        global worlds
        worlds = data

    async def setup_hook(self):
        global worlds
        await tournament_store.refresh()
        worlds = tournament_store.data
        if tournament_store.last_error:
            logger.warning(
                "Using %s tournament data (last successful fetch: %s)",
                tournament_store.source, tournament_store.fetched_at or "never",
            )

        guild = discord.Object(id=GUILD_ID)

        self.tree.copy_global_to(guild=guild)

        synced = await self.tree.sync(guild=guild)

        logger.info(
            "Synced %s command(s) to guild %s",
            len(synced),
            GUILD_ID,
        )
        self.background_polling.start()

    async def close(self):
        await self.background_polling.stop()
        await super().close()


bot = WorldsBot()


@bot.event
async def on_ready():
    logger.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id,
    )


# --------------------------------------------------
# Setup helpers
# --------------------------------------------------


def build_setup_content(
    user_id: int,
    display_name: str,
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

    all_games_server_ping = get_all_games_server_ping(user_id)

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
        all_games_text = "**On** " "*(5 minutes before match start)*"
    else:
        all_games_text = "**Off**"

    if followed_team_server_ping:
        followed_ping_text = "**On** " "*(5 minutes before match start)*"
    else:
        followed_ping_text = "**Off**"

    return (
        f"## Worlds Bot Setup for @{safe_display_name}\n\n"
        f"🌍 Timezone:\n"
        f"{timezone_text}\n\n"
        f"🕐 Time format:\n{time_format_text}\n\n"
        f"⭐ Teams you are following:\n"
        f"**{team_text}**\n\n"
        f"🔔 Server pings for all games:\n"
        f"{all_games_text}\n\n"
        f"📣 Server pings for followed teams:\n"
        f"{followed_ping_text}\n\u200b"
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
            **message_payload("## Choose your timezone\n\n"
                f"Region: **{region}**\n\n"
                "Choose the closest city.", controls=TimezoneCityView(region)),

        )


class TimezoneRegionView(discord.ui.View):
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
            **message_payload(build_setup_content(
                interaction.user.id,
                interaction.user.display_name,
            ), controls=SetupView(interaction.user.id)),

        )


class TimezoneCityView(discord.ui.View):
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
        followed_team_ids = get_followed_team_ids(user_id)

        teams = sorted(
            [team for team in worlds.teams if team.id != TBD_TEAM_ID],
            key=lambda team: (team.short_name),
        )

        options = [
            discord.SelectOption(
                label=team.name,
                description=(team.short_name),
                value=team.id,
                default=(team.id in followed_team_ids),
            )
            for team in teams
        ]

        super().__init__(
            placeholder=("Choose teams to follow..."),
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        selected_teams = set(self.values)

        set_followed_team_ids(
            interaction.user.id,
            selected_teams,
        )

        await interaction.response.edit_message(
            **message_payload(build_setup_content(
                interaction.user.id,
                interaction.user.display_name,
            ), controls=SetupView(interaction.user.id)),

        )


class TeamSelectView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
    ):
        super().__init__(timeout=900)

        self.add_item(TeamSelect(user_id))


# --------------------------------------------------
# Main setup view
# --------------------------------------------------


class TimeFormatSelect(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = user_id
        current = get_user_time_format(user_id)
        super().__init__(placeholder="Choose your time format", row=3, options=[
            discord.SelectOption(label="24-hour: 1:00h (CEST)", value="24h", default=current == "24h"),
            discord.SelectOption(label="12-hour: 1:00 am (CEST)", value="12h", default=current == "12h"),
        ])

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/setup` to change your own settings."), ephemeral=True)
            return
        set_user_time_format(interaction.user.id, self.values[0])
        await interaction.response.edit_message(**message_payload(
            build_setup_content(interaction.user.id, interaction.user.display_name),
            controls=SetupView(interaction.user.id)))


class SetupView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
    ):
        super().__init__(timeout=900)

        self.user_id = user_id
        self.add_item(TimeFormatSelect(user_id))

        (
            _,
            followed_team_server_ping,
            _,
        ) = get_notification_settings(user_id)

        all_games_server_ping = get_all_games_server_ping(user_id)

        if all_games_server_ping:
            self.toggle_all_games_ping.label = "Turn Server Pings for All Games OFF"

            self.toggle_all_games_ping.style = discord.ButtonStyle.success
        else:
            self.toggle_all_games_ping.label = "Turn Server Pings for All Games ON"

            self.toggle_all_games_ping.style = discord.ButtonStyle.secondary

        if followed_team_server_ping:
            self.toggle_server_ping.label = "Turn Server Pings for Your Teams OFF"

            self.toggle_server_ping.style = discord.ButtonStyle.success
        else:
            self.toggle_server_ping.label = "Turn Server Pings for Your Teams ON"

            self.toggle_server_ping.style = discord.ButtonStyle.secondary

    @discord.ui.button(
        label="Change Timezone",
        style=discord.ButtonStyle.secondary,
        emoji="🌍",
        row=0,
    )
    async def change_timezone(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            **message_payload("## Choose your timezone\n\n" "First choose your region.", controls=TimezoneRegionView()),

        )

    @discord.ui.button(
        label="Change Followed Teams",
        style=discord.ButtonStyle.secondary,
        emoji="⭐",
        row=0,
    )
    async def edit_teams(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            **message_payload("## Choose teams to follow\n\n"
                "Select any teams you want "
                "to follow.", controls=TeamSelectView(interaction.user.id)),

        )

    @discord.ui.button(
        label="All-Game Pings",
        style=discord.ButtonStyle.secondary,
        emoji="🔔",
        row=1,
    )
    async def toggle_all_games_ping(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        currently_enabled = get_all_games_server_ping(interaction.user.id)

        set_all_games_server_ping(
            interaction.user.id,
            not currently_enabled,
        )

        await interaction.response.edit_message(
            **message_payload(build_setup_content(
                interaction.user.id,
                interaction.user.display_name,
            ), controls=SetupView(interaction.user.id)),

        )

    @discord.ui.button(
        label="Followed-Team Pings",
        style=discord.ButtonStyle.secondary,
        emoji="📣",
        row=2,
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
            **message_payload(build_setup_content(
                interaction.user.id,
                interaction.user.display_name,
            ), controls=SetupView(interaction.user.id)),

        )

# --------------------------------------------------
# User requirements
# --------------------------------------------------


async def require_timezone(
    interaction: discord.Interaction,
) -> str | None:
    timezone_name = get_user_timezone(interaction.user.id)

    if timezone_name:
        return timezone_name

    await interaction.response.send_message(
        **message_payload("## Set your timezone first\n\n"
        "Run `/setup` to set your timezone, or choose your region below. "
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
    current = current.lower()

    matches = [
        team
        for team in worlds.teams
        if team.id != TBD_TEAM_ID
        if (current in team.name.lower() or current in team.short_name.lower())
    ]

    return [
        app_commands.Choice(
            name=(f"{team.name} " f"({team.short_name})"),
            value=team.id,
        )
        for team in matches[:25]
    ]


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
    description="Developer test for reminder targeting.",
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
    description="Simulate a reminder using the current tournament data.",
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


@bot.tree.command(name="serversetup", description="Choose the server's reminder channel and public notification settings.")
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
    name="setup",
    description=("Set up your Worlds preferences."),
)
async def setup(
    interaction: discord.Interaction,
):
    await interaction.response.defer(ephemeral=True)

    await interaction.edit_original_response(
        **message_payload(build_setup_content(
            interaction.user.id,
            interaction.user.display_name,
        ), controls=SetupView(interaction.user.id)),

    )


@bot.tree.command(
    name="next",
    description=("Show the next Worlds match."),
)
async def next_match_command(
    interaction: discord.Interaction,
):
    timezone_name = TOURNAMENT_TIMEZONE
    now = datetime.now(UTC)
    match = get_next_match(
        worlds,
        now,
    )

    if match is None:
        await interaction.response.send_message(
            **message_payload("There are no upcoming matches " "in the current tournament data.")
        )

        return

    message = format_next_match(
        match,
        timezone_name,
        now,
        timezones=get_registered_timezones() or [TOURNAMENT_TIMEZONE],
    )

    await interaction.response.send_message(**message_payload(message))


@bot.tree.command(name="previous", description="Show the last two completed Worlds matches with spoiler-hidden results.")
async def previous(interaction: discord.Interaction):
    now = datetime.now(UTC)
    matches = get_previous_matches(worlds, now)
    if not matches:
        await interaction.response.send_message(**message_payload("## __Previous Worlds Matches__\n\nNo completed Worlds matches yet."))
        return
    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]
    lines = []
    last_date = None
    for match in matches:
        match_date = local_datetime(match.start_time, TOURNAMENT_TIMEZONE).date()
        if match_date != last_date:
            lines.append(f"## __{format_local_date(match.start_time, TOURNAMENT_TIMEZONE)}__")
            last_date = match_date
        lines.append(format_match(match, TOURNAMENT_TIMEZONE, include_date=False, now=now,
                                  timezones=timezones, hide_result=True, small_details=False))
    lines.append("-# Results are hidden. Click a spoiler to reveal the result.")
    await send_long_message(interaction, "# __Previous Worlds Matches__", lines)


async def show_day_schedule(interaction: discord.Interaction, day_offset: int):
    timezone_name = TOURNAMENT_TIMEZONE
    now = datetime.now(UTC)
    target_date = now.astimezone(ZoneInfo(timezone_name)).date() + timedelta(days=day_offset)
    matches = get_matches_for_local_date(worlds, target_date, timezone_name)
    timezones = get_registered_timezones() or [timezone_name]
    label = "Today" if day_offset == 0 else "Tomorrow"
    title = f"__{worlds.tournament.year} Worlds: {label}__"
    date_label = f"{target_date.strftime('%a')}, {target_date.day} {target_date.strftime('%b %Y')}"
    context = f"__{date_label}__\n\n"
    descriptions = []
    current = context
    for match in matches:
        text = format_match(match, timezone_name, include_date=False, now=now, timezones=timezones)
        if len(current) + len(text) + 2 > 3800 and current != context:
            descriptions.append(current)
            current = context
        current += text + "\n\n"
    descriptions.append(current if matches else context + "No Worlds matches scheduled for this day.")
    for index, description in enumerate(descriptions):
        embed = discord.Embed(title=title, description=description.rstrip(), colour=PREDICTION_BLUE)
        if index == 0:
            await interaction.response.send_message(**embed_card_payload(embed), ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.followup.send(**embed_card_payload(embed), ephemeral=False, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="today", description="Show today's Worlds matches in all setup timezones.")
async def today(interaction: discord.Interaction):
    await show_day_schedule(interaction, 0)


@bot.tree.command(name="tomorrow", description="Show tomorrow's Worlds matches in all setup timezones.")
async def tomorrow(interaction: discord.Interaction):
    await show_day_schedule(interaction, 1)


@bot.tree.command(
    name="schedule",
    description=("Show the Worlds match schedule."),
)
@app_commands.describe(
    view="Choose the next five games or a full tournament stage",
)
@app_commands.choices(view=[
    app_commands.Choice(name="Next 5 games", value="next-five"),
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
        view_title = "Next 5 games"
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
            "Play-ins": "Play-ins Stage",
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

            lines.append(f"## __{local_date}__")

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
    )


@bot.tree.command(
    name="team",
    description=("Show a team's Worlds matches."),
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

    matches = get_matches_for_team(
        worlds,
        selected_team.id,
    )

    matches = sorted(
        matches,
        key=lambda match: (match.start_time),
    )

    header = f"## {selected_team.name}\n" f"**Region:** " f"{selected_team.region}"

    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]
    lines = []
    current_date = None
    for match in matches:
        match_date = local_datetime(match.start_time, TOURNAMENT_TIMEZONE).date()
        if match_date != current_date:
            lines.append(f"## __{format_local_date(match.start_time, TOURNAMENT_TIMEZONE)}__")
            current_date = match_date
        lines.append(format_match(match, timezone_name, include_date=False, timezones=timezones))

    await send_long_message(
        interaction,
        header,
        lines,
    )


@bot.tree.command(
    name="standings",
    description=("Show the Worlds Swiss standings."),
)
async def standings(
    interaction: discord.Interaction,
):
    swiss_stage = next(
        stage for stage in worlds.stages if ("swiss" in stage.name.lower())
    )

    table = get_standings(
        worlds,
        swiss_stage.id,
    )

    if not table:
        await interaction.response.send_message(**message_payload("No standings are available."))

        return

    lines = []

    for standing in table:
        team = worlds.teams_by_id[standing.team_id]

        lines.append(
            f"**{team.short_name}** · "
            f"{standing.wins}"
            f"-{standing.losses}"
        )

    message = (
        f"## Worlds {worlds.tournament.year}: Swiss Standings\n"
        "-# Series records from match results; no official tiebreak ranking.\n\n"
        + "\n".join(lines)
    )

    await interaction.response.send_message(**message_payload(message))


@bot.tree.command(name="pickems", description="goto pickems leaderboard")
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


@bot.tree.command(name="changevote", description="Make or change your match prediction.")
@app_commands.guild_only()
async def changevote(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    manager = getattr(interaction.client, "prediction_test_manager", None) or interaction.client.voting_manager
    try:
        poll = await manager.personal_vote_target(interaction.user.id)
    except ValueError as error:
        await interaction.followup.send(**message_payload(str(error)), ephemeral=True)
        return
    if poll is None:
        await interaction.followup.send(**message_payload("## __Your currently active predictions__\n\nVoting is closed for now. Use `/today` or `/tomorrow` to see the schedule."), ephemeral=True)
        return
    match = manager.store.data.matches_by_id[poll.match_id]
    saved = await asyncio.to_thread(get_user_votes, poll.guild_id, poll.tournament_id, interaction.user.id)
    selected_team = next((chosen for saved_poll, chosen in saved
                          if saved_poll.match_id == poll.match_id
                          and {saved_poll.team1_id, saved_poll.team2_id} == {poll.team1_id, poll.team2_id}), None)
    timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
    time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
    text = change_vote_text(match, now=manager.clock(), selected_team=selected_team,
                            test_mode=getattr(manager, "test_mode", False), timezone_name=timezone_name, time_format=time_format)
    payload = message_payload(text,
                              controls=PersonalVoteView(manager, poll, interaction.user.id, selected_team=selected_team))
    await interaction.followup.send(**payload,
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="currentvotes", description="See how everyone voted for upcoming and recent Worlds matches.")
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
    cards = []
    for match in matches:
        stage = data.stages_by_id.get(match.stage_id)
        try:
            rows = await asyncio.to_thread(get_upcoming_match_votes, interaction.guild_id, match.id)
        except ValueError:
            voting_closed = match.status != MatchStatus.SCHEDULED or now >= match.start_time - timedelta(minutes=5)
            no_votes = voting_closed and not await asyncio.to_thread(has_match_votes, interaction.guild_id, match.id)
            cards.append(hidden_votes_payload(match, now=now, stage_name=stage.name if stage else None, no_votes=no_votes))
        else:
            cards.append({"embed": upcoming_votes_embed(match, rows, stage_name=stage.name if stage else None)})
    for payload in group_voting_messages(cards):
        await interaction.followup.send(**payload, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="myvotes", description="View your predictions and see which were correct.")
@app_commands.guild_only()
async def myvotes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = worlds
    rows = await asyncio.to_thread(get_user_votes, interaction.guild_id, data.tournament.id, interaction.user.id, include_open=True)
    timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
    time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
    for embed in my_votes_embeds(rows, data, display_name=interaction.user.display_name,
                                timezone_name=timezone_name, time_format=time_format):
        await interaction.followup.send(**embed_card_payload(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="leaderboard", description="See Communism's Worlds prediction standings.")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await asyncio.to_thread(get_competition_stats, interaction.guild_id, worlds.tournament.id)
    lines = []
    rank = 0
    last_key = None
    visible_count = 0
    for row in rows:
        member = interaction.guild.get_member(row.user_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(row.user_id)
            except discord.NotFound:
                continue
        visible_count += 1
        if row.ranking_key != last_key:
            rank = visible_count
        last_key = row.ranking_key
        name = discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))
        streak = f"🔥 Current streak: {row.current_streak}" if row.current_streak >= 3 else f"Current streak: {row.current_streak}"
        lines.append(f"`{rank}.` **{name}**: {row.correct} correct\n"
                     f"-# {row.correct}/{row.submitted} · {float(row.accuracy):.1%} accuracy\n"
                     f"-# {row.submitted}/{row.eligible} matches predicted · {float(row.participation):.1%} participation\n"
                     f"-# {streak} · Best streak: {row.longest_streak}")
    header = "## __Communism Worlds Leaderboard__"
    if not lines:
        lines = ["No results yet. The standings update as matches finish."]
    chunks = [header]
    for line in lines:
        if len(chunks[-1]) + len(line) + 1 > 1900:
            chunks.append(header)
        chunks[-1] += "\n" + line
    for chunk in chunks:
        await interaction.followup.send(**message_payload(chunk), allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(
    name="help",
    description=("Show the available bot commands."),
)
async def help_command(
    interaction: discord.Interaction,
):
    message = (
        "## LoL Worlds Bot\n\n"
        "`/next`: Next Worlds match\n"
        "`/previous`: Last two matches, with results hidden\n"
        "`/schedule`: Match schedule\n"
        "`/today` · `/tomorrow`: Quick daily schedules\n"
        "`/standings`: Current standings\n"
        "`/team`: Team matches\n"
        "`/setup`: Your personal setup\n"
        "`/pickems`: Official Pick'ems leaderboard\n"
        "`/leaderboard`: Communism's Worlds prediction standings\n"
        "`/myvotes`: Your predictions and whether you were correct\n"
        "`/changevote`: Make or change your prediction\n"
        "`/currentvotes`: See how everyone voted for current matches\n"
        "`/ping`: Check bot status"
    )

    await interaction.response.send_message(**message_payload(message))


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
        message = "You need Manage Server permission to use `/serversetup`."
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

bot.run(
    TOKEN,
    log_handler=None,
)
