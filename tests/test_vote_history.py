import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from services import vote_history as history
from services.polls import Poll
from services.tournament_data import load_tournament_data


def context(monkeypatch):
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    target = SimpleNamespace(id=8, name="target", display_name="Target")
    guild = SimpleNamespace(get_member=lambda user_id: target if user_id == 8 else None,
                            fetch_member=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")), emojis=[])
    interaction = SimpleNamespace(guild_id=123, guild=guild, user=SimpleNamespace(id=7),
        client=SimpleNamespace(voting_manager=SimpleNamespace(store=SimpleNamespace(data=data))),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(history, "get_voter_display_names", lambda *args: {8: "Target", 9: "Gone"})
    monkeypatch.setattr(history, "get_user_timezone", lambda *args: "Europe/Berlin")
    monkeypatch.setattr(history, "get_user_time_format", lambda *args: "24h")
    return interaction, data, target


def test_voter_suggestions_exclude_nonmembers_and_filter_names(monkeypatch):
    interaction, _, _ = context(monkeypatch)
    choices = asyncio.run(history.voter_autocomplete(interaction, ""))
    assert [(choice.name, choice.value) for choice in choices] == [("Target (@target)", "8")]
    assert asyncio.run(history.voter_autocomplete(interaction, "nobody")) == []


def test_history_hides_open_picks_paginates_and_stats_use_selected_voter(monkeypatch):
    interaction, data, target = context(monkeypatch)
    now = datetime.now(UTC)
    first = data.matches[0]
    matches = [replace(first, id=f"past-{i}", start_time=now - timedelta(days=i+1)) for i in range(7)]
    matches.append(replace(first, id="secret-open", start_time=now + timedelta(days=5)))
    data = replace(data, matches=matches)
    interaction.client.voting_manager.store.data = data
    rows = [(Poll(123, match.id, data.tournament.id, match.team1_id, match.team2_id,
                  match.start_time, False, None), match.team1_id) for match in matches]
    monkeypatch.setattr(history, "get_user_votes", lambda *args: rows)
    stats = AsyncMock()
    monkeypatch.setattr(history, "show_personal_stats", stats)
    async def scenario():
        await history.show_vote_history(interaction, "8")
        view = interaction.followup.send.call_args.kwargs["view"].controls
        assert len(view.pages) == 2
        assert sum(field.name != "\u200b" for page in view.pages for field in page.fields) == 7
        assert all("Their pick:" in field.value for page in view.pages for field in page.fields if field.name != "\u200b")
        assert "Target" in view.pages[0].title
        await view.next_page.callback(interaction)
        assert view.page == 1
        await view.stats.callback(interaction)
        stats.assert_awaited_once_with(interaction, 123, data.tournament.id, target_user=target)
    asyncio.run(scenario())


def test_history_rejects_arbitrary_nonvoter_ids(monkeypatch):
    interaction, _, _ = context(monkeypatch)
    asyncio.run(history.show_vote_history(interaction, "123456"))
    assert "Choose a server member" in interaction.followup.send.call_args.kwargs["view"].children[0].children[0].content


def test_history_defaults_to_caller_even_without_votes_and_offers_my_stats(monkeypatch):
    interaction, data, _ = context(monkeypatch)
    interaction.user.display_name = "Lua"
    monkeypatch.setattr(history, "get_user_votes", lambda *args: [])
    stats = AsyncMock()
    monkeypatch.setattr(history, "show_personal_stats", stats)

    async def scenario():
        await history.show_vote_history(interaction)
        payload = interaction.followup.send.call_args.kwargs
        assert payload["ephemeral"]
        controls = payload["view"].controls
        assert controls.target is interaction.user
        assert controls.stats.label == "My Stats"
        await controls.stats.callback(interaction)
        stats.assert_awaited_once_with(interaction, 123, data.tournament.id, target_user=interaction.user)
    asyncio.run(scenario())
