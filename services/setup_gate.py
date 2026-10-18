"""Require personal setup across slash commands and component entry points."""

import asyncio
import discord
from discord import app_commands

from services.user_settings import has_completed_setup


async def require_setup(interaction):
    if getattr(interaction, "extras", {}).get("setup_verified"):
        return True
    ready = getattr(interaction, "guild_id", None) is not None and await asyncio.to_thread(
        has_completed_setup, interaction.guild_id, interaction.user.id)
    if ready:
        if hasattr(interaction, "extras"):
            interaction.extras["setup_verified"] = True
        return True
    if getattr(interaction, "type", None) == discord.InteractionType.autocomplete:
        await interaction.response.autocomplete([])
    else:
        from services.presentation import message_payload
        await interaction.response.send_message(**message_payload(
            "## __Set Up Your Worlds Bot First__\n\n"
            "Run `/mysetup` in this server to choose your timezone, teams, and notification preferences.\n\n"
            "Then you can vote and use all the other commands."), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
    return False


class SetupCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if (interaction.data or {}).get("name") == "mysetup":
            return True
        return await require_setup(interaction)


class SetupRequiredView(discord.ui.View):
    async def interaction_check(self, interaction):
        return await require_setup(interaction)


class SetupRequiredLayoutView(discord.ui.LayoutView):
    async def interaction_check(self, interaction):
        return await require_setup(interaction)


class SetupRequiredModal(discord.ui.Modal):
    async def interaction_check(self, interaction):
        return await require_setup(interaction)
