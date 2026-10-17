import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from services import user_settings
from services.server_settings import initialize_server_settings, get_server_settings, set_worlds_role, set_reminder_channel
from services.worlds_role import toggle_worlds_subscription, worlds_subscription_enabled
from services.server_setup import WorldsPingRoleSelect


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "roles.db")
    user_settings.initialize_database()
    initialize_server_settings()


def test_role_settings_survive_migration_and_other_setting_changes():
    set_worlds_role(123, 789)
    set_reminder_channel(123, 456)
    initialize_server_settings()
    settings = get_server_settings(123)
    assert settings.worlds_role_id == 789 and settings.reminder_channel_id == 456
    assert get_server_settings(124).worlds_role_id is None


def test_admin_selects_existing_role_without_role_creation_permission():
    role = SimpleNamespace(id=789, is_default=lambda: False, managed=False, permissions=discord.Permissions.none())
    guild = SimpleNamespace(id=123, get_role=Mock(return_value=role))
    async def scenario():
        select = WorldsPingRoleSelect(123)
        select._values = [role]
        interaction = SimpleNamespace(guild_id=123, guild=guild,
                                      user=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)),
                                      response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await select.callback(interaction)
        interaction.response.edit_message.assert_awaited_once()
    asyncio.run(scenario())
    assert get_server_settings(123).worlds_role_id == 789


def test_admin_cannot_select_a_privileged_role_for_self_assignment():
    role = SimpleNamespace(id=789, is_default=lambda: False, managed=False, permissions=discord.Permissions.all())
    async def scenario():
        select = WorldsPingRoleSelect(123)
        select._values = [role]
        interaction = SimpleNamespace(guild_id=123, guild=SimpleNamespace(get_role=lambda _: role),
                                      user=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await select.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
    asyncio.run(scenario())
    assert get_server_settings(123).worlds_role_id is None


def test_members_can_join_and_leave_only_the_configured_role():
    set_worlds_role(123, 789)
    role = SimpleNamespace(id=789, mention="<@&789>", is_assignable=lambda: True,
                           is_default=lambda: False, managed=False, permissions=discord.Permissions.none())
    member = SimpleNamespace(id=7, roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())
    guild = SimpleNamespace(id=123, get_role=Mock(return_value=role), fetch_member=AsyncMock(return_value=member))
    interaction = SimpleNamespace(guild_id=123, guild=guild, user=member,
                                  response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    async def scenario():
        assert not worlds_subscription_enabled(member, guild)
        assert await toggle_worlds_subscription(interaction)
        member.add_roles.assert_awaited_once_with(role, reason="Enabled server pings for all Worlds games")
        member.roles = [role]
        assert worlds_subscription_enabled(member, guild)
        assert not await toggle_worlds_subscription(interaction)
        member.remove_roles.assert_awaited_once_with(role, reason="Disabled server pings for all Worlds games")
    asyncio.run(scenario())
    assert all(call.args == (789,) for call in guild.get_role.call_args_list)


def test_role_dropdown_rechecks_admin_before_saving():
    async def scenario():
        select = WorldsPingRoleSelect(123)
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(roles=[]),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await select.callback(interaction)
        assert get_server_settings(123).worlds_role_id is None
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(scenario())


@pytest.mark.parametrize("privileged, assignable", [(True, True), (False, False)])
def test_all_games_toggle_cannot_assign_privileged_or_unmanageable_roles(privileged, assignable):
    set_worlds_role(123, 789)
    role = SimpleNamespace(id=789, is_default=lambda: False, managed=False,
                           permissions=discord.Permissions.all() if privileged else discord.Permissions.none(),
                           is_assignable=lambda: assignable)
    member = SimpleNamespace(id=7, roles=[], add_roles=AsyncMock())
    guild = SimpleNamespace(id=123, get_role=lambda _: role, fetch_member=AsyncMock(return_value=member))
    interaction = SimpleNamespace(guild=guild, user=member)
    with pytest.raises(ValueError):
        asyncio.run(toggle_worlds_subscription(interaction))
    member.add_roles.assert_not_awaited()
