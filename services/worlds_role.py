"""The configured Worlds role is the single subscription for all-game pings."""

import discord
import asyncio
from collections import defaultdict

from services.server_settings import get_server_settings
from services.user_settings import worlds_role_initialized, mark_worlds_role_initialized

_onboarding_locks = defaultdict(asyncio.Lock)


def configured_worlds_role(guild):
    if guild is None:
        return None
    role_id = get_server_settings(guild.id).worlds_role_id
    return guild.get_role(role_id) if role_id else None


def worlds_subscription_enabled(member, guild):
    role = configured_worlds_role(guild)
    return bool(role and any(item.id == role.id for item in getattr(member, "roles", ())))


async def toggle_worlds_subscription(interaction, *, enabled=None):
    role = configured_worlds_role(interaction.guild)
    if role is None:
        raise ValueError("The Worlds role hasn't been configured. Ask a server manager to select it in `/serversetup`.")
    if role.is_default() or role.managed or role.permissions.value:
        raise ValueError("Select a notification role without server permissions in `/serversetup`.")
    if not role.is_assignable():
        raise ValueError("I need Manage Roles permission, with my role above the Worlds notification role.")
    try:
        member = await interaction.guild.fetch_member(interaction.user.id)
        enabled = not any(item.id == role.id for item in member.roles) if enabled is None else enabled
        if enabled:
            await member.add_roles(role, reason="Enabled server pings for all Worlds games")
        else:
            await member.remove_roles(role, reason="Disabled server pings for all Worlds games")
    except discord.HTTPException as error:
        raise ValueError("I couldn't update your Worlds role. Check my Manage Roles permission and role position, then try again.") from error
    await asyncio.to_thread(mark_worlds_role_initialized, interaction.guild_id, interaction.user.id)
    return enabled


async def initialize_worlds_subscription(interaction):
    """Default on once; later visits never undo a member's opt-out."""
    if interaction.guild is None or configured_worlds_role(interaction.guild) is None:
        return None
    key = (interaction.guild_id, interaction.user.id)
    async with _onboarding_locks[key]:
        if await asyncio.to_thread(worlds_role_initialized, *key):
            return None
        member = await interaction.guild.fetch_member(interaction.user.id)
        if not worlds_subscription_enabled(member, interaction.guild):
            await toggle_worlds_subscription(interaction, enabled=True)
        await asyncio.to_thread(mark_worlds_role_initialized, *key)
        return True


async def individual_subscribers(guild, user_ids, role_id=None):
    recipients = []
    for user_id in sorted(set(user_ids)):
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            continue
        if not role_id or not any(role.id == role_id for role in member.roles):
            recipients.append(user_id)
    return recipients
