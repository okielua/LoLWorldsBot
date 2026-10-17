import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import golgg_players
from services.golgg import parse_table


def sample_table(tournament="Worlds 2026 Play-In"):
    return (f"<select id='cbtournament'><option value='{tournament}' selected>Worlds</option></select>"
            "<table class='playerslist'><tr><th>Player</th><th>Games</th><th>Win rate</th><th>KDA</th>"
            "<th>Avg kills</th><th>Avg deaths</th><th>Avg assists</th><th>KP%</th></tr>"
            f"<tr><td><a href='./player-stats/123/season-ALL/split-ALL/tournament-{tournament}/'>Blaber</a></td>"
            "<td>9</td><td>33.3%</td><td>3.5</td><td>3</td><td>2.7</td><td>6.2</td><td>75.1%</td></tr></table>")


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(golgg_players, "_catalogs", {2026: {"123": "Blaber", "234": "Ayu", "345": "Yike"}})


def test_player_parser_requires_matching_tournament_and_profile_links():
    rows = parse_table(sample_table(), "Worlds 2026 Play-In", kind="players")
    assert rows[0][0]["Player"] == "Blaber"
    assert rows[0][1].startswith("https://gol.gg/players/player-stats/123/")
    assert parse_table(sample_table("MSI 2026"), "Worlds 2026 Play-In", kind="players") == []


def test_catalog_is_alphabetical_and_case_insensitive():
    assert [name for _, name in golgg_players.player_catalog(2026)] == ["Ayu", "Blaber", "Yike"]
    assert golgg_players.player_catalog(2026, "BLA") == [("123", "Blaber")]


def test_player_reply_uses_worlds_only_stats_and_direct_player_link(monkeypatch):
    rows = parse_table(sample_table(), "Worlds 2026 Play-In", kind="players")
    fetch = AsyncMock(side_effect=[[], rows])
    monkeypatch.setattr(golgg_players, "fetch_table", fetch)
    text = asyncio.run(golgg_players.player_reply("123", 2026))
    assert "Blaber · Worlds 2026 Stats" in text
    assert "KDA: **3.5**" in text and "Average Kills: **3**\nAverage Deaths: **2.7**\nAverage Assists: **6.2**" in text
    assert "### __General Stats__" in text and "### __Aggression__" in text
    assert "75.1%" in text and "player-stats/123/" in text
    assert "tournament-Worlds%202026%20Play-In/" in text
    assert all(call.kwargs == {"kind": "players"} for call in fetch.call_args_list)


def test_missing_name_never_fetches_or_posts_player_list(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(golgg_players, "fetch_table", fetch)
    text = asyncio.run(golgg_players.player_reply(None, 2026))
    fetch.assert_not_awaited()
    assert "required name" in text and "Blaber" not in text


def test_unavailable_source_keeps_profile_link_and_does_not_invent_stats(monkeypatch):
    monkeypatch.setattr(golgg_players, "fetch_table", AsyncMock(return_value=None))
    text = asyncio.run(golgg_players.player_reply("blaber", 2026))
    assert "Stats temporarily unavailable" in text
    assert "player-stats/123/" in text and "KDA:" not in text


def test_unknown_player_is_not_substituted_with_another_player(monkeypatch):
    monkeypatch.setattr(golgg_players, "fetch_table", AsyncMock(return_value=[]))
    text = asyncio.run(golgg_players.player_reply("Missing player", 2026))
    assert "Player not found" in text and "player-stats/" not in text
