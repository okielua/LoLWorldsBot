"""Let members opt into the existing notification role selected by an Admin."""

import discord

from services.presentation import message_payload
from services.server_settings import get_server_settings


class WorldsRoleSelect(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = user_id
        super().__init__(placeholder="Choose whether to receive daily Worlds pings...", options=[
            discord.SelectOption(label="Get the Worlds 2026 Role", value="join", emoji="🔔"),
            discord.SelectOption(label="Remove the Worlds 2026 Role", value="leave", emoji="🔕"),
        ])

    async def callback(self, interaction):
        if interaction.user.id != self.user_id or not interaction.guild:
            await interaction.response.send_message(**message_payload("Use `/mysetup` in the server to change your own role."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        role = interaction.guild.get_role(get_server_settings(interaction.guild_id).worlds_role_id)
        if role is None:
            text = "The Worlds role hasn't been configured. Ask an Admin to run `/serversetup`."
        elif role.is_default() or role.managed or role.permissions.value:
            text = "The selected role isn't a notification-only role. Ask an Admin to select a role without server permissions in `/serversetup`."
        elif not role.is_assignable():
            text = "I can't manage the Worlds role. An Admin must give me Manage Roles and place my role above it."
        else:
            try:
                if self.values[0] == "join":
                    await interaction.user.add_roles(role, reason="Opted into Worlds daily posts")
                    text = f"You now have {role.mention} and will be pinged for daily Worlds posts."
                else:
                    await interaction.user.remove_roles(role, reason="Opted out of Worlds daily posts")
                    text = f"Removed {role.mention}. You won't receive its daily Worlds pings."
            except discord.HTTPException:
                text = "I couldn't update your role. Check my Manage Roles permission and role position, then try again."
        await interaction.followup.send(**message_payload(text), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
