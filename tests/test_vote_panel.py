import asyncio
from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services import polls
from services.vote_panel import OpenVotePanel, PublicVotersButton
from services.voting import DailyPredictionsView, personal_vote_payloads
from services.match_times import TOURNAMENT_TIMEZONE
from presentation_assertions import rendered_message
from test_voting import context


@pytest.mark.parametrize("receipt", ["today", "other_day", "other_guild", "other_tournament", "suppressed", None])
def test_today_links_only_its_daily_post_and_never_publishes_or_pings(context, receipt):
    from test_bot_commands import day_callback
    from services.vote_panel import OpenVotePanel
    manager, store, channel, now, _ = context
    now[0] = store.data.matches[0].start_time - timedelta(hours=1)
    store.fetched_at = now[0]
    manager.client.voting_manager = manager
    day = now[0].astimezone(ZoneInfo(TOURNAMENT_TIMEZONE)).date().isoformat()
    if receipt:
        polls.save_daily_post(999 if receipt == "other_guild" else 123,
                              "other" if receipt == "other_tournament" else store.data.tournament.id,
                              "2020-01-01" if receipt == "other_day" else day, 456, 987, "unchanged")
        if receipt == "suppressed":
            polls.set_daily_post_suppressed(123, store.data.tournament.id, day, True)
    interaction = SimpleNamespace(guild_id=123, client=manager.client,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def scenario():
        await day_callback(store.data, now[0])(interaction, 0)
        call = interaction.followup.send.call_args
        text = rendered_message(call).description
        if receipt == "today":
            assert "Open today's daily voting post" in text
            assert "https://discord.com/channels/123/456/987" in text
        else:
            assert "No daily voting post has been published for today yet." in text
            assert "https://discord.com/channels/" not in text
        assert call.kwargs["ephemeral"] is False
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert any(isinstance(item, OpenVotePanel) for item in call.kwargs["view"].walk_children())
        channel.send.assert_not_awaited()
        if receipt == "today":
            assert polls.get_daily_post(123, store.data.tournament.id, day) == (456, 987, "unchanged")
    asyncio.run(scenario())


def test_public_names_are_sorted_unique_and_never_include_choices(context):
    manager, store, channel, _, message = context
    first = store.data.matches[0]

    async def scenario():
        await manager.tick()
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        await manager.vote(first.id, 8, first.team2_id, "Synarch")
        await manager.vote(first.id, 7, first.team2_id, "Lua")
        assert polls.get_poll_participants(123, first.id) == [(7, "Lua"), (8, "Synarch")]
        assert polls.get_poll_participants(999, first.id) == []
        with pytest.raises(ValueError):
            polls.get_poll_results(123, first.id)
        await manager.refresh_participation(first.id)
        text = "\n".join(item.content for item in message.edit.call_args.kwargs["view"].walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
        assert "These users cast a vote:**\n@Lua\n@Synarch" in text
        assert text.count("@Lua") == text.count("@Synarch") == 1
        assert "Your pick" not in text and "Correct" not in text and "%" not in text
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        # A changed pairing must not list votes cast for the old pairing.
        with polls.get_connection() as db:
            db.execute("UPDATE match_polls SET team2_id='T1' WHERE guild_id=123 AND match_id=?", (first.id,))
        assert polls.get_poll_participants(123, first.id) == []
    asyncio.run(scenario())


def test_private_vote_panel_marks_only_callers_saved_pick_green(context):
    manager, store, _, _, _ = context

    async def scenario():
        await manager.tick()
        match = store.data.matches[0]
        await manager.vote(match.id, 7, match.team1_id, "Lua")
        await manager.vote(match.id, 8, match.team2_id, "Synarch")
        available = await manager.personal_vote_targets(7)
        saved = polls.get_user_votes(123, store.data.tournament.id, 7)
        payload = personal_vote_payloads(manager, available, 7, saved)[0]
        selected = [item for item in payload["view"].walk_children() if isinstance(item, discord.ui.Button)
                    and item.style == discord.ButtonStyle.success]
        assert len(selected) == 1
        assert selected[0].team_id == match.team1_id and selected[0].label.endswith(" ✓")
    asyncio.run(scenario())


def test_large_voter_lists_stay_bounded_with_public_full_list_fallback(context):
    manager, store, _, _, _ = context
    first = store.data.matches[0]
    matches = [replace(first, id=f"full-{i}") for i in range(8)]
    participants = {match.id: [(i, "Long user name " + str(i)) for i in range(300)] for match in matches}

    async def scenario():
        view = DailyPredictionsView(manager, "2026-10-15", matches, {}, participants=participants)
        assert view.total_children_count <= 40
        texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        assert sum(map(len, texts)) <= 4000
        assert "See All Voters" in "\n".join(texts)
        assert any(isinstance(item, PublicVotersButton) for item in view.walk_children())
    asyncio.run(scenario())


def test_public_button_opens_correct_private_panel_and_is_restart_safe(context):
    manager, store, _, _, _ = context
    callback = AsyncMock()
    other = SimpleNamespace(guild_id=123, store=SimpleNamespace(data=SimpleNamespace(tournament=SimpleNamespace(id="test"))))
    interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7), extras={"setup_verified": True},
        response=SimpleNamespace(send_message=AsyncMock()),
        client=SimpleNamespace(voting_manager=manager, prediction_test_manager=other,
                               tree=SimpleNamespace(get_command=lambda name: SimpleNamespace(callback=callback))))

    async def scenario():
        button = OpenVotePanel(123, store.data.tournament.id)
        restored = await OpenVotePanel.from_custom_id(interaction, button.item, button.template.fullmatch(button.custom_id))
        await restored.callback(interaction)
        callback.assert_awaited_once_with(interaction)
        assert interaction.extras["vote_panel_manager"] is manager
        interaction.guild_id = 999
        await restored.callback(interaction)
        callback.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"]
    asyncio.run(scenario())


def test_full_voter_list_is_public_and_paginated_without_choices_or_pings(context, monkeypatch):
    manager, store, _, _, _ = context
    monkeypatch.setattr(polls, "get_poll_participants", lambda *a: [(i, "Voter " + str(i)) for i in range(350)])
    interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7), extras={"setup_verified": True},
        client=SimpleNamespace(voting_manager=manager),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def scenario():
        button = PublicVotersButton(123, store.data.tournament.id)
        await button.callback(interaction)
        interaction.response.defer.assert_awaited_once_with()
        sent = interaction.followup.send.call_args.kwargs
        assert not sent.get("ephemeral", False)
        assert sent["allowed_mentions"].to_dict() == {"parse": []}
        pages = sent["view"].controls.pages
        assert len(pages) > 1 and all(len(page) <= 4000 for page in pages)
        assert "@Voter 349" in "\n".join(pages)
        assert "Your pick" not in "\n".join(pages)
    asyncio.run(scenario())
