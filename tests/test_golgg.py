import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services import golgg


def table(tournament="Worlds 2026 Play-In", selected=True):
    headers = ["Name", "Games", "Win rate", "Kills / game", "Deaths / game", "GD@15", "Game duration", "DRA%", "NASH%"]
    values = [f"<a href='./team-stats/123/split-ALL/tournament-{tournament}/'>Cloud9</a>", "9", "33.3%", "12.4", "14.5", "-500", "32:00", "40.0", "50.0"]
    return (f"<select id='cbtournament'><option value='{tournament}' {'selected' if selected else ''}>Tournament</option></select>"
            + "<table class='table_list playerslist'><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr><tr>"
            + "".join(f"<td>{v}</td>" for v in values) + "</tr></table>")


def test_parser_accepts_only_explicitly_filtered_worlds_rows():
    rows = golgg.parse_table(table(), "Worlds 2026 Play-In")
    assert rows[0][0]["Games"] == "9"
    assert rows[0][0]["GD@15"] == "-500"
    assert rows[0][1].startswith("https://gol.gg/teams/team-stats/123/")
    assert golgg.parse_table(table(selected=False), "Worlds 2026 Play-In") == []
    assert golgg.parse_table(table("MSI 2026"), "Worlds 2026 Play-In") == []


def test_worlds_summary_matches_alias_and_links_scoped_team_page(monkeypatch):
    rows = golgg.parse_table(table(), "Worlds 2026 Play-In")
    fetch = AsyncMock(side_effect=[[], rows])
    monkeypatch.setattr(golgg, "fetch_table", fetch)
    team = SimpleNamespace(id="C9", short_name="C9", name="Cloud9 Kia")
    text = asyncio.run(golgg.team_stats_text(team, 2026))
    assert "Worlds 2026 Play-In Stats" in text
    assert "Games: **9**" in text and "33.3%" in text
    assert "-500" in text and "Dragon Control: **40.0%**\nBaron Control: **50.0%**" in text
    assert "### __Economy__" in text and "### __Aggression__" in text
    assert "team-stats/123/split-ALL/tournament-Worlds%202026%20Play-In/" in text
    assert [call.args for call in fetch.call_args_list] == [("Worlds 2026 Main Event", 2026), ("Worlds 2026 Play-In", 2026)]


def test_positive_gold_difference_shows_explicit_plus(monkeypatch):
    rows = golgg.parse_table(table().replace("-500", "769"), "Worlds 2026 Play-In")
    monkeypatch.setattr(golgg, "fetch_table", AsyncMock(side_effect=[[], rows]))
    text = asyncio.run(golgg.team_stats_text(SimpleNamespace(id="C9", short_name="C9", name="Cloud9"), 2026))
    assert "Gold Difference at 15 Minutes: **+769**" in text


def test_no_worlds_stats_never_displays_season_figures(monkeypatch):
    rows = golgg.parse_table(table(), "Worlds 2026 Play-In")
    monkeypatch.setattr(golgg, "fetch_table", AsyncMock(side_effect=[[], []]))
    team = SimpleNamespace(id="C9", short_name="C9", name="Cloud9")
    text = asyncio.run(golgg.team_stats_text(team, 2026))
    assert "Not enough data" in text
    assert "33.3%" not in text and "Games: **9**" not in text
    assert "team-stats/2812/split-ALL/tournament-Worlds%202026%20Main%20Event/" in text


def test_source_failure_keeps_team_reply_available(monkeypatch):
    monkeypatch.setattr(golgg, "fetch_table", AsyncMock(return_value=None))
    text = asyncio.run(golgg.team_stats_text(SimpleNamespace(id="C9", short_name="C9", name="Cloud9"), 2026))
    assert "Stats temporarily unavailable" in text
    assert "View This Team’s Worlds Stats" in text
