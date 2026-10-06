import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv


# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("worldsbot")


# Check configuration
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing from .env")

GUILD_ID = int(GUILD_ID)


# Discord intents
intents = discord.Intents.default()


class WorldsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        # During development, copy commands to our server
        # so command changes appear immediately.
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


@bot.tree.command(
    name="ping",
    description="Check whether the Worlds bot is online.",
)
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)

    await interaction.response.send_message(
        f"🏓 Pong! `{latency_ms} ms`"
    )


@bot.tree.command(
    name="hello",
    description="Say hello to the Worlds bot.",
)
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Hi {interaction.user.mention}! 👋"
    )


@bot.tree.command(
    name="help",
    description="Show the available bot commands.",
)
async def help_command(interaction: discord.Interaction):
    message = (
        "**LoL Worlds Bot — Commands**\n\n"
        "`/ping` — Check whether the bot is online\n"
        "`/hello` — Say hello to the bot\n"
        "`/help` — Show this command list\n\n"
        "*Worlds features coming soon.*"
    )

    await interaction.response.send_message(message)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    logger.exception("Slash command error: %s", error)

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


bot.run(TOKEN, log_handler=None)