import asyncio
import re

import discord

from services.footnotes import format_footnotes
from services.leaderboard_display import sample_leaderboard_entries
from services.leaderboard_views import leaderboard_payload
from services.prediction_display import VOTE_CONFIRMATION_HELP, results_embed
from services.team_emojis import team_display


def test_footnotes_have_one_bullet_no_bold_and_keep_command_code():
    source = "Normal **heading**\n-# **Use** `/changevote` for *your prediction*.\n-# • Already bulleted\n-# - Legacy bullet"
    expected = "Normal **heading**\n-# *Use `/changevote` for your prediction.*\n\n-# • *Already bulleted*\n-# • *Legacy bullet*"
    assert format_footnotes(source) == expected
    assert format_footnotes(expected) == expected
    assert format_footnotes("-# **70% correct** · **Other text**") == "-# ***70% correct** · Other text*"
    bold = "-# *Voted on 24 matches · **75% correct***"
    assert format_footnotes(bold) == bold
    assert all(line.startswith("-# • ") and "**" not in line for line in VOTE_CONFIRMATION_HELP.splitlines())


def test_footnote_spacing_is_consistent_inside_text_and_after_controls():
    notes = "-# • Use `/changevote`.\n-# • Use `/currentvotes`."
    formatted = format_footnotes(notes)
    assert format_footnotes("Body\n" + notes) == "Body\n\n" + formatted
    assert format_footnotes("Body\n\n" + notes) == "Body\n\n" + formatted
    assert format_footnotes("Name\n-# Current streak: 3") == "Name\n-# *Current streak: 3*"
    displayed = team_display(notes).content
    assert displayed == "\u200b\n\n" + formatted
    assert team_display(displayed).content == displayed


def test_leaderboard_uses_real_card_renderer_and_only_player_headers_are_bold():
    async def scenario():
        payload = leaderboard_payload(sample_leaderboard_entries("lua"), 123, "worlds-2026", preview=True)
        assert payload["embeds"] == [] and "embed" not in payload
        assert isinstance(payload["view"], discord.ui.LayoutView)
        texts = [item.content for item in payload["view"].walk_children() if isinstance(item, discord.ui.TextDisplay)]
        text, = texts
        assert text.startswith("## __TEST: Communism Worlds Leaderboard__")
        assert "### `1.` lua: 18 correct\n-# *Voted on" in text
        assert "Sample results. The real leaderboard is unchanged." in text
        assert "\u200b" not in text and "**75% correct**" in text
        notes = [line for line in text.splitlines() if line.startswith("-#")]
        assert notes and all(line.endswith("*") for line in notes)
        assert all(not line.startswith("-# •") for line in notes if "Voted on" in line or "Current streak:" in line)
        assert "-# • *Ranked by" in text
        assert len(text) <= 4000
        help_text = team_display("-# **Use** `/currentvotes` to see votes.").content
        assert help_text == "-# *Use `/currentvotes` to see votes.*"
        payload["view"].stop()
    asyncio.run(scenario())
    embed = results_embed("Prediction results\n**C9**: 0% (0 votes)\n\u200b\n**KC**: 0% (0 votes)\n\u200b")
    assert all(line.startswith("-# • ") and "**" not in line for line in embed.fields[-1].value.splitlines())
