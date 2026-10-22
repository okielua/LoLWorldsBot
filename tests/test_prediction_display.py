from dataclasses import replace
from pathlib import Path
from datetime import timedelta
import pytest
from services import match_times
from services import prediction_display

from models import MatchStatus
from services.polls import Poll
from services.prediction_display import my_votes_embeds, results_embed
from services.tournament_data import load_tournament_data


def history_values(embed):
    return [field.value for field in embed.fields if field.name != "\u200b"]


def first_history_value(embeds):
    return history_values(embeds[0])[0]


@pytest.fixture(autouse=True)
def no_registered_zones(monkeypatch):
    monkeypatch.setattr(match_times, "get_registered_timezones", lambda: [])


def test_history_distinguishes_correct_wrong_pending_and_cancelled():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    rows = [(replace(poll, closed=True, winner_id=match.team1_id), match.team1_id),
            (replace(poll, closed=True, winner_id=match.team2_id), match.team1_id),
            (replace(poll, closed=True), match.team1_id), (poll, match.team1_id)]
    embed, = my_votes_embeds(rows, data, now=match.start_time - timedelta(hours=2, minutes=6))
    values = history_values(embed)
    assert "Result: ||**__C9__** won||\nYour pick: **C9** ||(Correct :KingPeepo:)||" in values[0]
    assert "Result: ||**__KC__** won||\nYour pick: **C9** ||(Incorrect :peepoSad:)||" in values[1]
    assert [field.value for field in embed.fields if field.name == "\u200b" and field.value.startswith("## ")] == [
        "## __🏁 Past Games__", "## __🕒 Upcoming Games__"]
    assert "\n\n🔒 Voting closed\nYour pick: **C9**\n-# Awaiting result" in values[2]
    assert "\n\n🗳️ Voting open\nYour pick:" in values[3]
    assert values[3].endswith(
        "Your pick: **C9**\n-# (Voting closes in 2 hours and 1 minute)")
    assert all(value.startswith("**Play-ins · Bo5**\n") for value in values)
    assert embed.fields[-1].value == prediction_display.VOTE_CONFIRMATION_HELP
    assert "🔵" not in values[3]
    assert all(field.name == "__C9 vs KC__" for field in embed.fields if field.name != "\u200b")
    cancelled = replace(data, matches=[replace(match, status=MatchStatus.CANCELLED)])
    assert "CANCELLED" in first_history_value(my_votes_embeds([(poll, match.team1_id)], cancelled))
    assert "no longer valid" in first_history_value(my_votes_embeds([(poll, "OTHER")], data))


def test_entire_history_is_paged_within_discord_embed_limits():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, True, None)
    embeds = my_votes_embeds([(poll, match.team1_id)] * 46, data)
    assert len(embeds) == 5
    assert sum(sum(field.name != "\u200b" for field in e.fields) for e in embeds) == 46
    assert all(len(e) <= 6000 and len(e.fields) <= 25 for e in embeds)


def test_grouped_history_uses_guild_emojis_and_hides_results_for_both_winning_sides():
    import re
    import discord
    from types import SimpleNamespace
    from services.presentation import embed_card_payload
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    template = data.matches[0]
    now = template.start_time + timedelta(days=2)
    first = replace(template, id="first", status=MatchStatus.COMPLETED, winner_id=template.team1_id,
                    team1_score=3, team2_score=0)
    second = replace(template, id="second", start_time=template.start_time + timedelta(hours=1),
                     status=MatchStatus.COMPLETED, winner_id=template.team2_id, team1_score=1, team2_score=3)
    live = replace(template, id="live", start_time=now - timedelta(hours=1), status=MatchStatus.LIVE)
    future = replace(template, id="future", start_time=now + timedelta(hours=1))
    data = replace(data, matches=[future, live, second, first])
    def entry(match):
        return (Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id,
                     match.start_time, match.status != MatchStatus.SCHEDULED, match.winner_id), match.team1_id)
    guild = SimpleNamespace(emojis=[discord.PartialEmoji(name="KingPeepo", id=111111111111111111),
                                    discord.PartialEmoji(name="peepoSad", id=222222222222222222, animated=True)])
    embed, = my_votes_embeds([entry(match) for match in data.matches], data, now=now, guild=guild)
    values = history_values(embed)
    assert "Your pick: **C9** ||(Correct <:KingPeepo:111111111111111111>)||" in values[0]
    assert "Your pick: **C9** ||(Incorrect <a:peepoSad:222222222222222222>)||" in values[1]
    assert "Result: ||**__C9__** **3–0** **KC**||" in values[0]
    assert "Result: ||**C9** **1–3** **__KC__**||" in values[1]
    assert "__3–0__" not in values[0] and "__1–3__" not in values[1]
    text = '\n\n'.join(item.content for item in embed_card_payload(embed)['view'].walk_children()
                       if isinstance(item, discord.ui.TextDisplay))
    assert text.index("## __🏁 Past Games__") < text.index("## __🔴 Ongoing Games__") < text.index("## __🕒 Upcoming Games__")
    assert text.count("**C9** vs **KC**") == 4
    visible = re.sub(r"\|\|.*?\|\|", "", text)
    assert all(secret not in visible for secret in ("Correct", "Incorrect", "KingPeepo", "peepoSad", "3–0", "1–3"))
    assert "Correct" not in values[2] and "Correct" not in values[3]
    assert len(text) <= 4000


def test_unvoted_match_only_has_remaining_time_and_deadline_is_exact():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    cutoff = match.start_time - timedelta(minutes=5)
    embed, = my_votes_embeds([(poll, None)], data, now=cutoff - timedelta(seconds=30))
    assert history_values(embed)[0] == "**Play-ins · Bo5**\n\n🕒 UPCOMING in 6 minutes\n\nThu, 15 Oct 2026\n-# 11:00h (PT)\n\n🗳️ Voting open\n-# (Voting closes in less than a minute)"
    assert len(history_values(embed)) == 1
    closed, = my_votes_embeds([(poll, match.team1_id)], data, now=cutoff)
    assert "Awaiting result" in history_values(closed)[0]
    assert "/vote" not in history_values(closed)[0]
    assert "Voting closes in" not in history_values(closed)[0]


def test_upcoming_votes_only_command_hint_is_small_grey():
    from services.prediction_display import hidden_votes_payload
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    payload = hidden_votes_payload(match, now=match.start_time - timedelta(hours=2, minutes=20), stage_name="Play-In")
    text = payload["view"].children[0].children[0].content
    assert text.startswith("## Votes: **C9** vs **KC**\n\n**Play-ins · Bo5**\n\nThu, 15 Oct 2026\n\n")
    assert "\n\n**🗳️ Voting for this match closes in 2 hours and 15 minutes**\n\n" in text
    assert "**UTC:**" not in text
    assert [line for line in text.splitlines() if line.startswith("-# ")] == [
        "-# • *Use `/vote` to make or change your prediction for these games*"]
    assert "Votes are private until" not in text


@pytest.mark.parametrize("slot", ["team1_id", "team2_id"])
def test_tbd_match_shows_pending_voting_without_countdown_or_controls(slot):
    import discord
    from services.prediction_display import hidden_votes_payload
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    match = replace(data.matches[0], **{slot: "TBD"})
    async def scenario():
        controls = discord.ui.View()
        controls.add_item(discord.ui.Button(label="Vote"))
        now = match.start_time - timedelta(hours=22)
        payload = hidden_votes_payload(match, now=now, controls=controls)
        text = payload["view"].children[0].children[0].content
        assert "Voting will open once both teams are confirmed." in text
        assert "closes in" not in text and "/vote" not in text
        assert not any(isinstance(item, discord.ui.Button) for item in payload["view"].walk_children())
        closed = hidden_votes_payload(match, now=match.start_time)
        assert "Voting closed" in closed["view"].children[0].children[0].content
    import asyncio
    asyncio.run(scenario())


def test_results_embed_handles_large_voter_list_within_field_limit():
    embed = results_embed("Prediction results\n**C9**: 100% (200 votes)\n" + ", ".join(["@Friend"] * 200) + "\n**KC**: 0% (0 votes)\n\u200b")
    assert len(embed.fields[0].value) <= 1024
    assert "more voters" in embed.fields[0].value
    assert embed.fields[1].name == "**KC**"
    assert embed.fields[1].value == "0%\n(0 votes)\n\n\u200b"


@pytest.mark.parametrize("status", [MatchStatus.LIVE, MatchStatus.COMPLETED])
def test_votes_match_scores_are_spoilered(status):
    from services.prediction_display import upcoming_votes_embed
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = replace(data.matches[0], status=status, team1_score=1, team2_score=0)
    embed = upcoming_votes_embed(match, [])
    assert "||1–0||" in embed.fields[2].value
    if status == MatchStatus.COMPLETED:
        assert embed.fields[2].value.startswith("🏁 FINISHED")
        assert "FINAL" not in embed.fields[2].value


def test_personal_history_uses_one_zone_and_currentvotes_omits_city_times(monkeypatch):
    from services.prediction_display import upcoming_votes_embed
    zones = ["Europe/London", "Europe/Berlin", "Asia/Tokyo"]
    monkeypatch.setattr(match_times, "get_registered_timezones", lambda: zones)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, True, None)
    history = first_history_value(my_votes_embeds([(poll, match.team1_id)], data, timezone_name="Europe/Berlin"))
    public_embed = upcoming_votes_embed(match, [], stage_name="Play-In")
    assert public_embed.title is None
    assert public_embed.description == "## Votes: **C9** vs **KC**\n\n**Play-ins · Bo5**\n\nThu, 15 Oct 2026\n\nNo votes were made."
    assert all(field.inline for field in public_embed.fields[:2])
    assert public_embed.fields[2].inline is False
    public = public_embed.fields[2].value
    assert public == "🕒 UPCOMING"
    help_block = public_embed.fields[-1].value
    assert all(line.startswith("-# • *Use `/") and line.endswith("*") for line in help_block.splitlines())
    assert "`/votehistory`" in help_block and "`/leaderboard`" in help_block
    assert public_embed.fields[-1].inline is False
    assert public_embed.footer.text is None
    assert "**" not in help_block
    assert history.startswith("**Play-ins · Bo5**\n\n🕒 UPCOMING\n\nThu, 15 Oct 2026\n-# 20:00h (CEST)\n\n🔒 Voting closed\nYour pick:")
    assert all(city not in history for city in ("London", "Berlin", "Tokyo"))
    assert all(city not in public for city in ("London", "Berlin", "Tokyo"))


@pytest.mark.parametrize("zone,date,time", [
    ("Asia/Seoul", "Fri, 16 Oct 2026", "3:00h (KST)"),
    (None, "Thu, 15 Oct 2026", "11:00h (PT)"),
])
def test_private_voting_screens_use_personal_date_and_time(zone, date, time, monkeypatch):
    from services.prediction_display import change_vote_text
    monkeypatch.setattr(match_times, "get_registered_timezones", lambda: ["Europe/Berlin", "Europe/London"])
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    now = match.start_time - timedelta(hours=1)
    history = first_history_value(my_votes_embeds([(poll, match.team1_id)], data, now=now, timezone_name=zone))
    controls = change_vote_text(match, now=now, timezone_name=zone)
    assert f"{date}\n-# {time}\n\n🗳️ Voting open\n-# (Voting closes in 55 minutes)" in controls
    for text in (history, controls):
        assert date in text and time in text
        assert "Berlin" not in text and "London" not in text
        assert "(Voting closes in 55 minutes)" in text


def test_private_voting_screens_apply_twelve_hour_preference():
    from services.prediction_display import change_vote_text
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    now = match.start_time - timedelta(hours=1)
    history = first_history_value(my_votes_embeds([(poll, match.team1_id)], data, now=now,
                             timezone_name="Europe/Berlin", time_format="12h"))
    controls = change_vote_text(match, now=now, timezone_name="Europe/Berlin", time_format="12h")
    for text in (history, controls):
        assert "8:00 pm (CEST)" in text
        assert "20:00h" not in text


def test_grouped_result_messages_respect_embed_count_and_text_limits():
    import discord
    from services.prediction_display import group_voting_messages
    cards = [{"embed": discord.Embed(title=f"Match {index}", description="x" * 1500)} for index in range(12)]
    messages = group_voting_messages(cards)
    assert sum(len(message["embeds"]) for message in messages) == 12
    assert all(len(message["embeds"]) <= 10 and sum(len(embed) for embed in message["embeds"]) <= 6000
               for message in messages)
    short_cards = [{"embed": discord.Embed(title=f"Match {index}")} for index in range(12)]
    assert [len(message["embeds"]) for message in group_voting_messages(short_cards)] == [10, 2]
