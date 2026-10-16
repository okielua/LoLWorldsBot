"""The private administrator panel for server-wide reminder settings."""

import logging

import discord
from services.presentation import message_payload

from services.server_reminders import validate_reminder_channel
from services.reminder_test import run_reminder_test
from services.prediction_test import start_prediction_test
from services.server_settings import (
    get_server_settings, set_public_reminders_enabled, set_reminder_channel, set_pickems_url,
)


logger = logging.getLogger(__name__)


def build_server_setup_content(guild_id):
    settings = get_server_settings(guild_id)
    channel = f"<#{settings.reminder_channel_id}>" if settings.reminder_channel_id else "Not selected"
    status = "**On** *(5 minutes before match start)*" if settings.public_reminders_enabled else "**Off**"
    return (
        "## Server Setup\n\n"
        f"📣 Public match reminders:\n{status}\n\n"
        f"📍 Reminder channel:\n{channel}\n\n"
        "One public reminder per scheduled match with known teams, even when nobody is pinged.\n"
        "Personal ping preferences stay in `/setup`."
        f"\n\n🎯 Pick'ems leaderboard link: {'Set' if settings.pickems_url else 'Not set'}"
        + ("\n\nChoose a channel to turn public reminders on." if settings.reminder_channel_id is None else "")
    )


class ReminderChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Choose the reminder channel...",
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=0,
        )


    async def callback(self, interaction):
        await interaction.response.defer()
        try:
            channel_id = self.values[0].id
            channel = interaction.client.get_channel(channel_id)
            if channel is None:
                channel = await interaction.client.fetch_channel(channel_id)
            validate_reminder_channel(channel, self.guild_id)
        except (discord.HTTPException, ValueError) as error:
            logger.warning("Reminder channel selection failed: %s", error)
            message = str(error) if isinstance(error, ValueError) else "I couldn't access that channel. Check the bot's channel permissions."
            await interaction.followup.send(**message_payload(message), ephemeral=True)
            return
        set_reminder_channel(self.guild_id, channel.id)
        await interaction.edit_original_response(
            **message_payload(build_server_setup_content(self.guild_id), controls=ServerSetupView(self.guild_id)),
        )


class PickemsLinkModal(discord.ui.Modal, title="Official Pick'ems leaderboard"):
    link = discord.ui.TextInput(label="HTTPS leaderboard link", max_length=500)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id
        self.link.default = get_server_settings(guild_id).pickems_url

    async def on_submit(self, interaction):
        if interaction.guild_id != self.guild_id or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(**message_payload("You need Manage Server permission."), ephemeral=True)
            return
        try:
            set_pickems_url(self.guild_id, self.link.value.strip())
        except ValueError as error:
            await interaction.response.send_message(**message_payload(str(error)), ephemeral=True)
            return
        await interaction.response.send_message(**message_payload("Pick'ems link saved. `/pickems` will now show it."), ephemeral=True)


class ServerSetupView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.add_item(ReminderChannelSelect(guild_id))
        settings = get_server_settings(guild_id)
        self.toggle_reminders.disabled = settings.reminder_channel_id is None
        self.toggle_reminders.label = (
            "Turn Public Reminders Off" if settings.public_reminders_enabled else "Turn Public Reminders On"
        )
        self.toggle_reminders.style = (
            discord.ButtonStyle.success if settings.public_reminders_enabled else discord.ButtonStyle.secondary
        )
        self.test_reminder.disabled = (
            settings.reminder_channel_id is None or not settings.public_reminders_enabled
        )
        self.test_predictions.disabled = self.test_reminder.disabled

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(**message_payload("You need Manage Server permission to change these settings."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Public Reminders", emoji="📣", row=1)
    async def toggle_reminders(self, interaction, button):
        settings = get_server_settings(self.guild_id)
        set_public_reminders_enabled(self.guild_id, not settings.public_reminders_enabled)
        await interaction.response.edit_message(
            **message_payload(build_server_setup_content(self.guild_id), controls=ServerSetupView(self.guild_id)),
        )

    @discord.ui.button(label="Send Test Reminder (No Pings)", emoji="🧪", row=2)
    async def test_reminder(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await run_reminder_test(
            interaction.client, interaction.client.background_polling.store, self.guild_id,
        )
        await interaction.followup.send(**message_payload(message), ephemeral=True)

    @discord.ui.button(label="Set Pick'ems Link", emoji="🎯", row=3)
    async def set_pickems_link(self, interaction, button):
        await interaction.response.send_modal(PickemsLinkModal(self.guild_id))

    @discord.ui.button(label="Test Daily Predictions (No Pings)", emoji="🧪", row=2)
    async def test_predictions(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await start_prediction_test(interaction, self.guild_id)
        await interaction.followup.send(**message_payload(message), ephemeral=True)

    async def on_error(self, interaction, error, item):
        logger.error("Server setup failed", exc_info=(type(error), error, error.__traceback__))
        message = "Something went wrong while saving the server settings. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(**message_payload(message), ephemeral=True)
        else:
            await interaction.response.send_message(**message_payload(message), ephemeral=True)
