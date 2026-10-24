"""Compatibility entry points: personal setup is optional throughout the bot."""

import discord
from discord import app_commands

async def require_setup(interaction):
    # Keep existing component callbacks and command-tree integrations compatible,
    # without a database lookup, response, or implicit settings/subscription write.
    return True


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
