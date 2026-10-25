import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from presentation_assertions import rendered_payload
from models import MatchStatus

from services import user_settings
from services.leaderboard_display import sample_leaderboard_entries
from services.leaderboard_views import MyStatsButton, leaderboard_payload, show_personal_stats
from services.polls import create_poll, cast_vote, initialize_polls, record_poll_opportunity, reconcile_polls
from services.tournament_data import load_tournament_data


@pytest.fixture
def stats_data(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "stats.db")
    user_settings.initialize_database()
    initialize_polls()
    for user_id in (7, 8):
        user_settings.mark_setup_completed(123, user_id)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    start = datetime(2026, 10, 16, 18, tzinfo=UTC)
    swiss = next(stage for stage in data.stages if "swiss" in stage.name.lower())
    matches = [replace(match, start_time=start + timedelta(hours=i), stage_id=swiss.id) for i, match in enumerate(data.matches[:2])]
    for i, match in enumerate(matches):
        create_poll(123, match)
        record_poll_opportunity(123, match.id, match.team1_id, match.team2_id, start - timedelta(hours=1))
        cast_vote(123, match.id, 7, match.team1_id, start - timedelta(minutes=20), "lua")
        cast_vote(123, match.id, 8, match.team2_id, start - timedelta(minutes=20), "Other user")
    completed = replace(matches[0], status=MatchStatus.COMPLETED, winner_id=matches[0].team1_id)
    data = replace(data, matches=[completed, matches[1]])
    reconcile_polls(123, data, start + timedelta(minutes=10))
    return data


def interaction_for(data, user_id):
    members = {7: SimpleNamespace(id=7, display_name="lua"), 8: SimpleNamespace(id=8, display_name="Other user")}
    return SimpleNamespace(
        guild_id=123, guild=SimpleNamespace(get_member=members.get), user=members[user_id],
        client=SimpleNamespace(voting_manager=SimpleNamespace(store=SimpleNamespace(data=data))),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_playin_votes_count_in_leaderboard_and_personal_stats_with_result_protection(stats_data, monkeypatch):
    from services import spoilers
    from services.prediction_stats import get_competition_snapshot
    from services.spoiler_delivery import render_payload

    playin = next(stage for stage in stats_data.stages if "play" in stage.name.lower())
    completed = replace(stats_data.matches[0], stage_id=playin.id)
    data = replace(stats_data, matches=[completed, stats_data.matches[1]])
    now = completed.start_time + timedelta(minutes=30)
    policy = spoilers.SpoilerPolicy(data, ended_at={completed.id: now}, clock=lambda: now)
    monkeypatch.setattr(spoilers, "_current", policy)
    snapshot = get_competition_snapshot(123, data.tournament.id, data=data)
    assert snapshot.result_match_ids == (completed.id,) and snapshot.eligible == 1
    assert policy.protected(completed)

    async def scenario():
        interaction = interaction_for(data, 7)
        from services.leaderboard_views import leaderboard_context
        _, entries, _ = await leaderboard_context(interaction, data.tournament.id, data=data)
        assert {row.user_id: (row.correct, row.submitted, row.eligible) for row, _ in entries} == {
            7: (1, 1, 1), 8: (0, 1, 1)}
        await show_personal_stats(interaction, 123, data.tournament.id, data=data, target_user=interaction.user)
        reply = interaction.followup.send.call_args.kwargs
        assert reply["ephemeral"] is True
        rendered = render_payload({"components": reply["view"].to_components()})
        text = "\n".join(child["content"] for card in rendered["components"] for child in card["components"]
                         if child.get("type") == 10)
        assert "**My correct predictions: ||1|| (||100%||)**" in text
        assert "__Check Your PayPal__" in text
        assert text.count("Not enough data") == 4
        assert 'including Play-Ins' in text
    asyncio.run(scenario())


def test_button_and_stats_command_return_identical_private_clicker_stats(stats_data):
    from test_bot_commands import load_command
    data = stats_data
    async def scenario():
        caller = interaction_for(data, 8)
        button = MyStatsButton(123, data.tournament.id)
        await button.callback(caller)
        clicked = caller.followup.send.call_args.kwargs
        rendered = rendered_payload(clicked)
        assert rendered.title == "__My Worlds Stats · Other user__"
        assert "**My correct predictions: 0 (0%)**" in rendered.description
        assert "My incorrect predictions: **1**" in rendered.description
        assert "Pending votes" not in rendered.description
        assert all(label in rendered.description for label in ("## :PeyzPal: __Check Your PayPal__", "## :PepeKMS: __Can’t Read These Frauds__", "## :Copium: __Hopium Supplier__", "## :Discheesed: __Democracy Was a Mistake__"))
        first_text = next(item.content for item in clicked["view"].walk_children() if isinstance(item, discord.ui.TextDisplay))
        assert first_text.startswith("# __My Worlds Stats · Other user__\n")
        caller.response.defer.assert_awaited_once_with(ephemeral=True)
        assert clicked["ephemeral"] is True
        assert clicked["allowed_mentions"].to_dict() == {"parse": []}
        command_caller = interaction_for(data, 8)
        await load_command("stats", data)(command_caller)
        assert rendered_payload(command_caller.followup.send.call_args.kwargs) == rendered
        owner = interaction_for(data, 7)
        await button.callback(owner)
        assert "**My correct predictions: 1 (100%)**" in rendered_payload(owner.followup.send.call_args.kwargs).description
    asyncio.run(scenario())


def test_stats_button_restores_scope_after_restart_and_rejects_other_guilds(stats_data):
    async def scenario():
        original = MyStatsButton(123, stats_data.tournament.id)
        caller = interaction_for(stats_data, 8)
        restored = await MyStatsButton.from_custom_id(caller, original.item, MyStatsButton.__discord_ui_compiled_template__.fullmatch(original.custom_id))
        assert restored.guild_id == 123 and restored.tournament_id == stats_data.tournament.id
        await restored.callback(caller)
        assert "Other user" in rendered_payload(caller.followup.send.call_args.kwargs).title
        other = interaction_for(stats_data, 8)
        other.guild_id = 999
        await restored.callback(other)
        other.response.send_message.assert_awaited_once()
        assert other.response.send_message.call_args.kwargs["ephemeral"] is True
        other.followup.send.assert_not_awaited()
    asyncio.run(scenario())


def test_normal_weight_card_keeps_my_stats_button_and_pagination(stats_data):
    entries = sample_leaderboard_entries("lua")
    async def scenario():
        payload = leaderboard_payload(entries, 123, stats_data.tournament.id)
        assert "embed" not in payload and payload["embeds"] == []
        assert isinstance(payload["view"], discord.ui.LayoutView)
        assert len(payload["view"].children) == 1
        button, = [item for item in payload["view"].walk_children() if isinstance(item, MyStatsButton)]
        assert button.item.label == "My Advanced Stats"
        assert button.item.style == discord.ButtonStyle.primary
        many = [(replace(row, user_id=i * 6 + row.user_id), f"Long sample player {i} " + "x" * 90)
                for i in range(30) for row, _ in entries]
        large = leaderboard_payload(many, 123, stats_data.tournament.id)
        view = large["view"].controls
        assert len(view.pages) > 1 and view.previous.disabled
        assert all(len(embed.description) <= 4096 and len(embed) <= 6000 for embed in view.pages)
        click = interaction_for(stats_data, 8)
        await view.next_page.callback(click)
        assert view.page == 1
        assert view.pages[1].description in rendered_payload(click.response.edit_message.call_args.kwargs).description
        assert click.response.edit_message.call_args.kwargs["embeds"] == []
        assert "embed" not in click.response.edit_message.call_args.kwargs
        assert len([item for item in click.response.edit_message.call_args.kwargs["view"].walk_children() if isinstance(item, MyStatsButton)]) == 1
        await view.previous.callback(click)
        assert view.page == 0 and view.previous.disabled
        view.stop()
        payload["view"].stop()
    asyncio.run(scenario())
