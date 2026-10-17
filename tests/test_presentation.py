import ast
import asyncio
from pathlib import Path

import pytest

from services.presentation import BLUE, message_payload
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["bot.py", "services/server_setup.py", "services/prediction_test.py", "services/voting.py", "services/leaderboard_views.py"])
def test_every_interaction_reply_uses_shared_embeds_or_explicit_rich_layout(filename):
    """Cover command replies, setup edits, errors and test reports, not just schedules."""
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"send_message", "send", "edit_message", "edit_original_response"}:
            continue
        if not ast.unparse(node.func).startswith("interaction."):
            continue
        assert not node.args, f"{filename}:{node.lineno}: plain reply bypasses presentation rules"
        assert not any(key.arg == "content" for key in node.keywords)
        if any(key.arg == "embed" for key in node.keywords):
            assert "upcoming_votes_embed" in ast.unparse(node), "Only column-based voting results use native embeds"
        assert any(key.arg in {"embed", "view"} or (
            key.arg is None and isinstance(key.value, ast.Call)
            and isinstance(key.value.func, ast.Name) and key.value.func.id in {"message_payload", "embed_card_payload", "hidden_votes_payload"}
        ) or (key.arg is None and isinstance(key.value, ast.Name) and key.value.id == "payload"
        ) for key in node.keywords), f"{filename}:{node.lineno}: missing rich presentation"


def test_shared_embed_has_one_underlined_title_and_keeps_times_normal():
    async def scenario():
        payload = message_payload("# Worlds Schedule\n\n-# **Berlin:** 20:00\n-# • Use `/currentvotes` for picks.")
        assert payload["content"] is None
        assert payload["embeds"] == []
        card, = payload["view"].children
        assert card.accent_colour.value == BLUE
        assert card.children[0].content == "## __Worlds Schedule__\n\n-# *Berlin: 20:00*\n\n-# • *Use `/currentvotes` for picks.*"
    asyncio.run(scenario())


def test_card_preserves_control_callbacks_and_permission_checks():
    async def scenario():
        controls = discord.ui.View(timeout=900)
        controls.interaction_check = AsyncMock(return_value=False)
        button = discord.ui.Button(label="Change setting")
        button.callback = AsyncMock()
        controls.add_item(button)
        payload = message_payload("# __Setup__\nNormal body", controls=controls)
        view = payload["view"]
        interaction = SimpleNamespace()
        assert not await view.interaction_check(interaction)
        controls.interaction_check.assert_awaited_once_with(interaction)
        saved_button, = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert saved_button is button
        await saved_button.callback(interaction)
        button.callback.assert_awaited_once_with(interaction)
        assert payload["content"] is None and payload["embeds"] == []
    asyncio.run(scenario())
