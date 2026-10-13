import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from dotenv import load_dotenv

from services.timezones import TIMEZONE_REGIONS, get_timezone_label
from services.tournament_data import load_tournament_data
from services.tournament_queries import (
    get_matches_for_stage,
    get_matches_for_team,
    get_next_match,
    get_standings,
)
from services.user_settings import (
    get_followed_team_ids,
    get_user_timezone,
    initialize_database,
    set_followed_team_ids,
    set_user_timezone,
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
DATA_FILE = BASE_DIR / "data" / "worlds_2025.json"

worlds = load_tournament_data(DATA_FILE)
initialize_database()

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
    local = local_datetime(dt, timezone_name)
    return f"{local.day} {local.strftime('%B %Y')}"


def format_local_time(
    dt: datetime,
    timezone_name: str,
) -> str:
    local = local_datetime(dt, timezone_name)
    timezone_abbreviation = local.tzname() or timezone_name

    return f"{local.strftime('%H:%M')} {timezone_abbreviation}"


def labels_are_duplicate(
    stage_name: str,
    round_name: str,
) -> bool:
    def normalize(value: str) -> str:
        return value.lower().replace(" ", "").replace("-", "").rstrip("s")

    return normalize(stage_name) == normalize(round_name)


# --------------------------------------------------
# Match formatting
# --------------------------------------------------


def format_match(
    match,
    timezone_name: str,
    include_date: bool = True,
) -> str:
    team1 = worlds.teams_by_id[match.team1_id]
    team2 = worlds.teams_by_id[match.team2_id]
    stage = worlds.stages_by_id[match.stage_id]

    local_date = format_local_date(
        match.start_time,
        timezone_name,
    )

    local_time = format_local_time(
        match.start_time,
        timezone_name,
    )

    parts = []

    if include_date:
        parts.append(f"### {local_date}")

    stage_details = [stage.name]

    if match.round_name and not labels_are_duplicate(
        stage.name,
        match.round_name,
    ):
        stage_details.append(match.round_name)

    stage_details.append(f"Bo{match.best_of}")

    parts.append(f"**{' · '.join(stage_details)}**")

    parts.append(f"`{local_time}` · " f"**{team1.short_name} vs {team2.short_name}**")

    if (
        match.status.value == "completed"
        and match.team1_score is not None
        and match.team2_score is not None
    ):
        parts.append(
            f"✅ **FINAL** · "
            f"{team1.short_name} "
            f"{match.team1_score}–{match.team2_score} "
            f"{team2.short_name}"
        )

    elif match.status.value == "live":
        if match.team1_score is not None and match.team2_score is not None:
            parts.append(
                f"🔴 **LIVE** · "
                f"{team1.short_name} "
                f"{match.team1_score}–{match.team2_score} "
                f"{team2.short_name}"
            )
        else:
            parts.append("🔴 **LIVE**")

    elif match.status.value == "postponed":
        parts.append("⚠️ **POSTPONED**")

    elif match.status.value == "cancelled":
        parts.append("⛔ **CANCELLED**")

    else:
        parts.append("🕒 **UPCOMING**")

    return "\n".join(parts)


def default_schedule_stage():
    now = datetime.now(UTC)
    next_match = get_next_match(worlds, now)

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

    for line in lines:
        addition = f"\n\n{line}"

        if len(current) + len(addition) > 1900:
            chunks.append(current)
            current = line
        else:
            current += addition

    if current:
        chunks.append(current)

    await interaction.response.send_message(chunks[0])

    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


# --------------------------------------------------
# Discord client
# --------------------------------------------------

intents = discord.Intents.default()


class WorldsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)

        logger.info(
            "Synced %s command(s) to guild %s",
            len(synced),
            GUILD_ID,
        )


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
) -> str:
    timezone_name = get_user_timezone(user_id)
    followed_team_ids = get_followed_team_ids(user_id)

    if timezone_name:
        timezone_label = get_timezone_label(timezone_name)

        timezone_text = f"**{timezone_label}** " f"(`{timezone_name}`)"
    else:
        timezone_text = "*Not set*"

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

        team_text = ", ".join(f"**{team.short_name}**" for team in followed_teams)

    else:
        team_text = "*None selected*"

    return (
        "## Worlds Bot Setup\n\n"
        f"🌍 **Timezone:** {timezone_text}\n"
        f"⭐ **Following:** {team_text}"
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
            placeholder="Choose your region...",
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
            content=(
                "## Choose your timezone\n\n"
                f"Region: **{region}**\n\n"
                "Choose the closest city."
            ),
            view=TimezoneCityView(region),
        )


class TimezoneRegionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

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
            for label, timezone_name in TIMEZONE_REGIONS[region]
        ]

        super().__init__(
            placeholder="Choose the closest city...",
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
            content=build_setup_content(interaction.user.id),
            view=SetupView(interaction.user.id),
        )


class TimezoneCityView(discord.ui.View):
    def __init__(
        self,
        region: str,
    ):
        super().__init__(timeout=180)

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
            worlds.teams,
            key=lambda team: team.short_name,
        )

        options = [
            discord.SelectOption(
                label=team.name,
                description=team.short_name,
                value=team.id,
                default=(team.id in followed_team_ids),
            )
            for team in teams
        ]

        super().__init__(
            placeholder="Choose teams to follow...",
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
            content=build_setup_content(interaction.user.id),
            view=SetupView(interaction.user.id),
        )


class TeamSelectView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
    ):
        super().__init__(timeout=180)

        self.add_item(TeamSelect(user_id))


# --------------------------------------------------
# Main setup view
# --------------------------------------------------


class SetupView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
    ):
        super().__init__(timeout=180)

        self.user_id = user_id

    @discord.ui.button(
        label="Change Timezone",
        style=discord.ButtonStyle.secondary,
        emoji="🌍",
    )
    async def change_timezone(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content=("## Choose your timezone\n\n" "First choose your region."),
            view=TimezoneRegionView(),
        )

    @discord.ui.button(
        label="Change Followed Teams",
        style=discord.ButtonStyle.secondary,
        emoji="⭐",
    )
    async def edit_teams(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content=(
                "## Choose teams to follow\n\n" "Select any teams you want to follow."
            ),
            view=TeamSelectView(interaction.user.id),
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
        "## Set your timezone first\n\n"
        "Choose your region below. "
        "The bot will use your timezone for all match times.",
        view=TimezoneRegionView(),
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
        if (current in team.name.lower() or current in team.short_name.lower())
    ]

    return [
        app_commands.Choice(
            name=f"{team.name} ({team.short_name})",
            value=team.id,
        )
        for team in matches[:25]
    ]


async def stage_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    current = current.lower()

    matches = [stage for stage in worlds.stages if current in stage.name.lower()]

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
    description="Check whether the Worlds bot is online.",
)
async def ping(
    interaction: discord.Interaction,
):
    latency_ms = round(bot.latency * 1000)

    await interaction.response.send_message(f"🏓 Pong! `{latency_ms} ms`")


@bot.tree.command(
    name="setup",
    description="Set up your Worlds preferences.",
)
async def setup(
    interaction: discord.Interaction,
):
    await interaction.response.send_message(
        build_setup_content(interaction.user.id),
        view=SetupView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(
    name="next",
    description="Show the next Worlds match.",
)
async def next_match_command(
    interaction: discord.Interaction,
):
    timezone_name = await require_timezone(interaction)

    if timezone_name is None:
        return

    match = get_next_match(
        worlds,
        datetime.now(UTC),
    )

    if match is None:
        await interaction.response.send_message(
            "There are no upcoming matches " "in the current tournament data."
        )
        return

    formatted_match = format_match(
        match,
        timezone_name,
    )

    message = "## Next Worlds Match\n\n" f"{formatted_match}"

    await interaction.response.send_message(message)


@bot.tree.command(
    name="schedule",
    description="Show the Worlds match schedule.",
)
@app_commands.describe(
    stage="Optional tournament stage",
)
@app_commands.autocomplete(stage=stage_autocomplete)
async def schedule(
    interaction: discord.Interaction,
    stage: str | None = None,
):
    timezone_name = await require_timezone(interaction)

    if timezone_name is None:
        return

    if stage is None:
        selected_stage = default_schedule_stage()

    else:
        selected_stage = worlds.stages_by_id.get(stage)

        if selected_stage is None:
            await interaction.response.send_message(
                "I couldn't find that stage.",
                ephemeral=True,
            )
            return

    matches = get_matches_for_stage(
        worlds,
        selected_stage.id,
    )

    matches = sorted(
        matches,
        key=lambda match: match.start_time,
    )

    if not matches:
        await interaction.response.send_message(
            f"No matches found for {selected_stage.name}."
        )
        return

    header = f"# {worlds.tournament.year} Worlds Schedule"

    lines = []
    current_date = None

    for match in matches:
        local_match_time = local_datetime(
            match.start_time,
            timezone_name,
        )

        match_date = local_match_time.date()

        if match_date != current_date:
            current_date = match_date

            local_date = format_local_date(
                match.start_time,
                timezone_name,
            )

            lines.append(f"## {local_date}")

        lines.append(
            format_match(
                match,
                timezone_name,
                include_date=False,
            )
        )

    await send_long_message(
        interaction,
        header,
        lines,
    )


@bot.tree.command(
    name="team",
    description="Show a team's Worlds matches.",
)
@app_commands.describe(
    team="Choose a Worlds team",
)
@app_commands.autocomplete(team=team_autocomplete)
async def team(
    interaction: discord.Interaction,
    team: str,
):
    timezone_name = await require_timezone(interaction)

    if timezone_name is None:
        return

    selected_team = worlds.teams_by_id.get(team.upper())

    if selected_team is None:
        await interaction.response.send_message(
            "I couldn't find that team.",
            ephemeral=True,
        )
        return

    matches = get_matches_for_team(
        worlds,
        selected_team.id,
    )

    matches = sorted(
        matches,
        key=lambda match: match.start_time,
    )

    header = f"## {selected_team.name}\n" f"**Region:** {selected_team.region}"

    lines = [
        format_match(
            match,
            timezone_name,
        )
        for match in matches
    ]

    await send_long_message(
        interaction,
        header,
        lines,
    )


@bot.tree.command(
    name="standings",
    description="Show the Worlds Swiss standings.",
)
async def standings(
    interaction: discord.Interaction,
):
    swiss_stage = next(
        stage for stage in worlds.stages if "swiss" in stage.name.lower()
    )

    table = get_standings(
        worlds,
        swiss_stage.id,
    )

    if not table:
        await interaction.response.send_message("No standings are available.")
        return

    lines = []

    for standing in table:
        team = worlds.teams_by_id[standing.team_id]

        rank = standing.rank if standing.rank is not None else "-"

        lines.append(
            f"`{str(rank).rjust(2)}.` "
            f"**{team.short_name}** "
            f"{standing.wins}-{standing.losses}"
        )

    message = "## Worlds 2025 — Swiss Standings\n\n" + "\n".join(lines)

    await interaction.response.send_message(message)


@bot.tree.command(
    name="help",
    description="Show the available bot commands.",
)
async def help_command(
    interaction: discord.Interaction,
):
    message = (
        "## LoL Worlds Bot\n\n"
        "`/next` — Next Worlds match\n"
        "`/schedule` — Match schedule\n"
        "`/standings` — Current standings\n"
        "`/team` — Team matches\n"
        "`/setup` — Your timezone and followed teams\n"
        "`/ping` — Check bot status"
    )

    await interaction.response.send_message(message)


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

    message = "Something went wrong while running that command."

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )

    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


# --------------------------------------------------
# Start bot
# --------------------------------------------------

bot.run(
    TOKEN,
    log_handler=None,
)
