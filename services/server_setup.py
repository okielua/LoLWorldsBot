"""The private administrator panel for server-wide reminder settings."""

import logging

import discord
from services.presentation import message_payload
from services.runtime_config import development_enabled

from services.server_reminders import validate_reminder_channel
from services.reminder_test import run_reminder_test
from services.prediction_test import start_prediction_test
from services.leaderboard_display import sample_leaderboard_entries
from services.leaderboard_views import leaderboard_payload
from services.server_settings import (
    get_server_settings, set_public_reminders_enabled, set_reminder_channel, set_pickems_url, set_worlds_role,
)


logger = logging.getLogger(__name__)


def can_manage_server(interaction, guild_id):
    permissions = getattr(interaction.user, "guild_permissions", None)
    return interaction.guild_id == guild_id and bool(
        permissions and (permissions.manage_guild or getattr(permissions, "administrator", False))
    )


def build_server_setup_content(guild_id):
    settings = get_server_settings(guild_id)
    channel = f"<#{settings.reminder_channel_id}>" if settings.reminder_channel_id else "Not selected"
    status = "**On** *(5 minutes before match start)*" if settings.public_reminders_enabled else "**Off**"
    role = f"<@&{settings.worlds_role_id}>" if settings.worlds_role_id else "Not selected"
    return (
        "## Server Setup\n\n"
        f"### 📣 __Public Match Reminders:__\n{status}\n"
        "One public reminder per scheduled match with known teams, even when nobody is pinged.\n"
        "Personal ping preferences stay in `/mysetup`.\n\n"
        f"### 📍 __Reminder Channel:__\n{channel}"
        + ("\nChoose a channel to turn public reminders on." if settings.reminder_channel_id is None else "")
        + f"\n\n### 🔔 __All-Game Worlds Ping Role:__\n{role}\n"
        "Choose a notification role without server permissions. Make it mentionable for daily posts and 5-minute match reminders.\n"
        "Members join or leave this role using Server Pings for All Games in `/mysetup`.\n\n"
        + ("### 🧪 __Test Posts:__\nPreview without pings, or send a five-minute test using members' `/mysetup` ping preferences.\n\n"
           if development_enabled() else "")
        + "### 🏆 __Leaderboards:__\nSet the official Pick'ems link"
        + (" or preview the bot's prediction leaderboard." if development_enabled() else ".")
    )


class ReminderChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Choose the reminder channel...",
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1,
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


class PickemsLinkModal(discord.ui.Modal, title="Official Pick'ems Leaderboard"):
    link = discord.ui.TextInput(label="HTTPS leaderboard link", max_length=500)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id
        self.link.default = get_server_settings(guild_id).pickems_url

    async def on_submit(self, interaction):
        if not can_manage_server(interaction, self.guild_id):
            await interaction.response.send_message(**message_payload("You need the Manage Server permission."), ephemeral=True)
            return
        try:
            set_pickems_url(self.guild_id, self.link.value.strip())
        except ValueError as error:
            await interaction.response.send_message(**message_payload(str(error)), ephemeral=True)
            return
        await interaction.response.send_message(**message_payload("Pick'ems link saved. `/pickems` will now show it."), ephemeral=True)


class WorldsPingRoleSelect(discord.ui.RoleSelect):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(placeholder="Choose the role for all Worlds game pings...", min_values=1, max_values=1, row=2)

    async def callback(self, interaction):
        if not can_manage_server(interaction, self.guild_id):
            await interaction.response.send_message(**message_payload("You need the Manage Server permission."), ephemeral=True)
            return
        role = interaction.guild.get_role(self.values[0].id)
        if role is None or role.is_default() or role.managed or role.permissions.value:
            await interaction.response.send_message(**message_payload(
                "Choose a notification role without server permissions. Members can give themselves this role in `/mysetup`."), ephemeral=True)
            return
        set_worlds_role(self.guild_id, role.id)
        await interaction.response.edit_message(**message_payload(build_server_setup_content(self.guild_id),
                                                controls=ServerSetupView(self.guild_id)),
                                                allowed_mentions=discord.AllowedMentions.none())


class ServerSetupView(discord.ui.View):
    control_after_paragraphs = (2, 3, 4, 5, 6)

    def __init__(self, guild_id):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.add_item(ReminderChannelSelect(guild_id))
        self.add_item(WorldsPingRoleSelect(guild_id))
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
        self.test_reminder_with_pings.disabled = self.test_reminder.disabled
        if not development_enabled():
            for item in (self.test_reminder, self.test_reminder_with_pings, self.test_predictions, self.preview_leaderboard):
                self.remove_item(item)
            self.control_after_paragraphs = (2, 3, 4, 5)

    async def interaction_check(self, interaction):
        if not can_manage_server(interaction, self.guild_id):
            await interaction.response.send_message(**message_payload("You need the Manage Server permission to change these settings."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Public Reminders", emoji="📣", row=0)
    async def toggle_reminders(self, interaction, button):
        settings = get_server_settings(self.guild_id)
        set_public_reminders_enabled(self.guild_id, not settings.public_reminders_enabled)
        await interaction.response.edit_message(
            **message_payload(build_server_setup_content(self.guild_id), controls=ServerSetupView(self.guild_id)),
        )

    @discord.ui.button(label="Send Test Reminder (No Pings)", emoji="🧪", row=3)
    async def test_reminder(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await run_reminder_test(
            interaction.client, interaction.client.background_polling.store, self.guild_id,
        )
        await interaction.followup.send(**message_payload(message), ephemeral=True)

    @discord.ui.button(label="Test 5-Min Reminder (With Pings)", emoji="🔔", row=3)
    async def test_reminder_with_pings(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await run_reminder_test(
            interaction.client, interaction.client.background_polling.store, self.guild_id, ping_users=True,
        )
        await interaction.followup.send(**message_payload(message), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Set Pick'ems Link", emoji="🎯", row=4)
    async def set_pickems_link(self, interaction, button):
        await interaction.response.send_modal(PickemsLinkModal(self.guild_id))

    @discord.ui.button(label="Test Daily Predictions (No Pings)", emoji="🧪", row=3)
    async def test_predictions(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await start_prediction_test(interaction, self.guild_id)
        await interaction.followup.send(**message_payload(message), ephemeral=True)

    @discord.ui.button(label="Preview Leaderboard", emoji="🏆", row=4)
    async def preview_leaderboard(self, interaction, button):
        if not development_enabled():
            await interaction.response.send_message(**message_payload("Sample previews are disabled in production."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        entries = sample_leaderboard_entries(interaction.user.display_name)
        payload = leaderboard_payload(entries, self.guild_id, interaction.client.voting_manager.store.data.tournament.id,
                                      previous={1: 2, 2: 1, 3: 2, 4: 5, 5: 4, 6: 6}, preview=True)
        await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction, error, item):
        logger.error("Server setup failed", exc_info=(type(error), error, error.__traceback__))
        message = "Something went wrong while saving the server settings. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(**message_payload(message), ephemeral=True)
        else:
            await interaction.response.send_message(**message_payload(message), ephemeral=True)
