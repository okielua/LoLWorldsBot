import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services import user_settings
from services.presentation import message_payload
from services.setup_gate import SetupCommandTree, SetupRequiredView, SetupRequiredLayoutView, SetupRequiredModal, require_setup


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "setup.db")
    user_settings.initialize_database()


def interaction(name="help", guild_id=123):
    return SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=7, display_name="Lua"),
                           data={"name": name}, extras={}, type=discord.InteractionType.application_command,
                           response=SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock(), defer=AsyncMock()),
                           edit_original_response=AsyncMock())


def test_all_commands_allow_members_without_personal_setup():
    async def scenario():
        client = discord.Client(intents=discord.Intents.none())
        tree = SetupCommandTree(client)
        for name in ("help", "vote", "today", "schedule", "leaderboard", "votehistory", "currentvotes", "serversetup", "myvotes", "ping"):
            caller = interaction(name)
            assert await tree.interaction_check(caller)
            caller.response.send_message.assert_not_awaited()
        assert await tree.interaction_check(interaction("mysetup"))
        user_settings.mark_setup_completed(123, 7)
        assert await tree.interaction_check(interaction())
        assert await tree.interaction_check(interaction(guild_id=456))
        assert not user_settings.has_completed_setup(456, 7)
        await client.close()
    asyncio.run(scenario())


def test_setup_gate_does_not_intercept_autocomplete():
    caller = interaction("player")
    caller.type = discord.InteractionType.autocomplete
    assert asyncio.run(require_setup(caller))
    caller.response.autocomplete.assert_not_awaited()
    caller.response.send_message.assert_not_awaited()


def test_timezone_falls_back_without_a_setup_prompt():
    from test_bot_commands import load_command
    from services.match_times import TOURNAMENT_TIMEZONE
    callback = load_command('require_timezone', None, get_user_timezone=lambda _: None)
    caller = interaction('vote')
    assert asyncio.run(callback(caller)) == TOURNAMENT_TIMEZONE
    caller.response.send_message.assert_not_awaited()


def test_views_modals_and_cards_allow_first_timers_without_creating_settings():
    async def scenario():
        for view in (SetupRequiredView(), SetupRequiredLayoutView(), SetupRequiredModal(title="Settings"),
                     message_payload("## Other Panel")["view"]):
            assert await view.interaction_check(interaction())
        controls = discord.ui.View()
        controls.setup_controls = True
        assert await message_payload("## My Setup", controls=controls)["view"].interaction_check(interaction())
    asyncio.run(scenario())
    assert not user_settings.has_completed_setup(123, 7)
    assert not user_settings.worlds_role_initialized(123, 7)
    assert user_settings.get_user_timezone(7) is None


def test_mysetup_records_access_only_after_panel_is_presented():
    from test_bot_commands import load_command
    initialize = AsyncMock(return_value=None)
    callback = load_command("setup", None, asyncio=asyncio, discord=discord,
                            initialize_worlds_subscription=initialize,
                            mark_setup_completed=user_settings.mark_setup_completed,
                            setup_payload=lambda *args, **kwargs: message_payload("## My Setup"))
    caller = interaction("mysetup")
    asyncio.run(callback(caller))
    assert user_settings.has_completed_setup(123, 7)
    caller.user.id = 8
    caller.edit_original_response.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
    with pytest.raises(discord.NotFound):
        asyncio.run(callback(caller))
    assert not user_settings.has_completed_setup(123, 8)


def test_existing_personal_setup_is_preserved_by_migration_and_restart():
    user_settings.mark_worlds_role_initialized(123, 7)
    user_settings.initialize_database()
    assert user_settings.has_completed_setup(123, 7)
    assert not user_settings.has_completed_setup(456, 7)
    user_settings.initialize_database()
    assert user_settings.has_completed_setup(123, 7)
