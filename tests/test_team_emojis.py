import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from services import team_emojis
from services.prediction_display import format_prediction_results, results_embed, upcoming_votes_embed, my_votes_embeds
from services.polls import Poll
from services.presentation import message_payload
from services.tournament_data import load_tournament_data
from services.voting import VoteButton, PersonalVoteView


@pytest.fixture
def logos():
    teams = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json").teams
    emojis = [discord.PartialEmoji(name=name, id=100000000000000000 + index, animated=name == "t1")
              for index, name in enumerate(sorted(set(team_emojis.EMOJI_NAMES.values())))]
    team_emojis.configure_team_emojis(emojis, teams)
    yield {emoji.name: emoji for emoji in emojis}
    team_emojis.configure_team_emojis(())


def test_every_known_team_and_full_name_gets_its_logo(logos):
    teams = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json").teams
    for team in teams:
        emoji = logos[team_emojis.EMOJI_NAMES[team.id]]
        for name in (team.id, team.short_name, team.name):
            assert team_emojis.team_text(name) == f"{emoji} {name}"
    assert team_emojis.team_text("FUR vs C9") == f"{logos['FURIA']} **FUR** vs **C9** {logos['CLOUD9']}"
    assert team_emojis.team_emoji("DK") == logos["DKIA"]


def test_logos_preserve_formatting_spoilers_links_and_usernames(logos):
    text = "||**__T1__** **3–0** **FUR**|| Use `/team T1` https://example.com/T1 @T1 <@123> PARTIAL"
    expected = f"||{logos['t1']} **__T1__** **3–0** **FUR** {logos['FURIA']}|| Use `/team T1` https://example.com/T1 @T1 <@123> PARTIAL"
    assert team_emojis.team_text(text) == expected
    assert team_emojis.team_text(expected) == expected
    assert team_emojis.team_text("TBD") == "TBD"


def test_actual_cards_results_and_controls_use_logos(logos):
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    match = data.matches[0]
    manager = SimpleNamespace(guild_id=123)
    poll = SimpleNamespace(guild_id=123, match_id=match.id, team1_id="T1", team2_id="C9", closed=False)

    async def scenario():
        card = message_payload("## T1 vs C9\nYour pick: **T1**")["view"]
        text = next(item.content for item in card.walk_children() if isinstance(item, discord.ui.TextDisplay))
        assert f"{logos['t1']} **T1** vs **C9** {logos['CLOUD9']}" in text
        button = VoteButton(manager, poll, 1)
        assert button.emoji == logos["t1"]
        assert button.label == "T1" and button.team_id == "T1"
        assert button.custom_id.endswith(":1")
        view = PersonalVoteView(manager, poll, 7, "C9")
        assert view.children[1].emoji == logos["CLOUD9"]
        assert view.children[1].label == "C9 ✓"
    asyncio.run(scenario())
    embed = results_embed(format_prediction_results(match, []))
    assert str(team_emojis.team_emoji(match.team1_id)) in embed.fields[0].name
    assert str(team_emojis.team_emoji(match.team2_id)) in embed.fields[1].name
    current = upcoming_votes_embed(match, [])
    assert current.title is None
    assert str(team_emojis.team_emoji(match.team1_id)) in current.description


def test_refresh_handles_removed_or_missing_emojis(logos):
    team_emojis.configure_team_emojis([logos["t1"]])
    assert team_emojis.team_text("T1 vs C9") == f"{logos['t1']} **T1** vs **C9**"
    assert team_emojis.team_emoji("C9") is None
    team_emojis.configure_team_emojis([])
    assert team_emojis.team_text("T1 vs C9") == "**T1** vs **C9**"


@pytest.mark.parametrize("text", ["C9 vs FUR", "**C9** vs **FUR**", "__C9 vs FUR__",
                                  "## __C9 vs FUR__", "## __Votes: C9 vs FUR__"])
def test_matchup_layout_has_outer_logos_and_no_whole_line_underline(logos, text):
    prefix = "## " if text.startswith("## ") else ""
    if "Votes:" in text:
        prefix += "Votes: "
    expected = f"{prefix}{logos['CLOUD9']} **C9** vs **FUR** {logos['FURIA']}"
    assert team_emojis.team_text(text) == expected
    assert team_emojis.team_text(expected) == expected


@pytest.mark.parametrize("winner", ["C9", "FUR"])
def test_even_winning_team_is_not_underlined_in_a_matchup(logos, winner):
    left = "**__C9__**" if winner == "C9" else "**C9**"
    right = "**__FUR__**" if winner == "FUR" else "**FUR**"
    expected = f"||{logos['CLOUD9']} **C9** vs **FUR** {logos['FURIA']}||"
    assert team_emojis.team_text(f"||{left} vs {right}||") == expected
    assert team_emojis.team_text(expected) == expected


def test_pick_emoji_has_a_space_and_follows_result_on_its_own_line(logos):
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    match = replace(data.matches[0], team1_id="MVK", team2_id="FUR", winner_id="MVK", team1_score=3, team2_score=0)
    data = replace(data, matches=[match])
    poll = Poll(123, match.id, match.tournament_id, "MVK", "FUR", match.start_time, True, "MVK")
    embed, = my_votes_embeds([(poll, "FUR")], data, now=datetime(2026, 10, 17, tzinfo=UTC))
    body = team_emojis.team_text(next(field.value for field in embed.fields if field.name != "\u200b"))
    assert f"Result: ||{logos['MVK']} **__MVK__** **3–0** **FUR** {logos['FURIA']}||\nYour pick: **{logos['FURIA']} FUR** ||(Incorrect" in body
    old = f"Your pick: **{logos['FURIA']}FUR**"
    assert team_emojis.team_text(old) == f"Your pick: **{logos['FURIA']} FUR**"
    assert team_emojis.team_text(body) == body


@pytest.mark.parametrize("text", ["## __C9 vs FUR__", "### **C9 vs FUR**", "**C9** **vs** **FUR**"])
def test_matchup_keeps_heading_size_without_explicitly_bolding_vs(logos, text):
    rendered = team_emojis.team_text(text)
    if text.startswith("#"):
        assert rendered.startswith(text.split(" ", 1)[0] + " ")
    assert " vs " in rendered
    assert all("vs" not in bold for bold in re.findall(r"\*\*(.*?)\*\*", rendered))
