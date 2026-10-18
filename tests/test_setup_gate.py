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


def test_commands_only_allow_mysetup_before_first_setup_and_unlock_afterwards():
    async def scenario():
        client = discord.Client(intents=discord.Intents.none())
        tree = SetupCommandTree(client)
        for name in ("help", "currentvotes", "serversetup", "myvotes", "ping"):
            caller = interaction(name)
            assert not await tree.interaction_check(caller)
            sent = caller.response.send_message.call_args.kwargs
            assert sent["ephemeral"] is True
            assert "Run `/mysetup`" in sent["view"].children[0].children[0].content
        assert await tree.interaction_check(interaction("mysetup"))
        user_settings.mark_setup_completed(123, 7)
        assert await tree.interaction_check(interaction())
        assert not await tree.interaction_check(interaction(guild_id=456))
        await client.close()
    asyncio.run(scenario())


def test_autocomplete_returns_no_suggestions_until_setup():
    caller = interaction("player")
    caller.type = discord.InteractionType.autocomplete
    assert not asyncio.run(require_setup(caller))
    caller.response.autocomplete.assert_awaited_once_with([])
    caller.response.send_message.assert_not_awaited()


def test_views_and_modals_block_first_timers_but_setup_controls_remain_accessible():
    async def scenario():
        for view in (SetupRequiredView(), SetupRequiredLayoutView(), SetupRequiredModal(title="Settings"),
                     message_payload("## Other Panel")["view"]):
            assert not await view.interaction_check(interaction())
        controls = discord.ui.View()
        controls.setup_controls = True
        assert await message_payload("## My Setup", controls=controls)["view"].interaction_check(interaction())
    asyncio.run(scenario())


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
