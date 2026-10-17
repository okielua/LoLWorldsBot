"""The configured Worlds role is the single subscription for all-game pings."""

import discord

from services.server_settings import get_server_settings


def configured_worlds_role(guild):
    if guild is None:
        return None
    role_id = get_server_settings(guild.id).worlds_role_id
    return guild.get_role(role_id) if role_id else None


def worlds_subscription_enabled(member, guild):
    role = configured_worlds_role(guild)
    return bool(role and any(item.id == role.id for item in getattr(member, "roles", ())))


async def toggle_worlds_subscription(interaction):
    role = configured_worlds_role(interaction.guild)
    if role is None:
        raise ValueError("The Worlds role hasn't been configured. Ask a server manager to select it in `/serversetup`.")
    if role.is_default() or role.managed or role.permissions.value:
        raise ValueError("Select a notification role without server permissions in `/serversetup`.")
    if not role.is_assignable():
        raise ValueError("I need Manage Roles permission, with my role above the Worlds notification role.")
    try:
        member = await interaction.guild.fetch_member(interaction.user.id)
        enabled = not any(item.id == role.id for item in member.roles)
        if enabled:
            await member.add_roles(role, reason="Enabled server pings for all Worlds games")
        else:
            await member.remove_roles(role, reason="Disabled server pings for all Worlds games")
    except discord.HTTPException as error:
        raise ValueError("I couldn't update your Worlds role. Check my Manage Roles permission and role position, then try again.") from error
    return enabled
