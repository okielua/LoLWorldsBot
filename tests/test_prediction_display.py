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
    assert "Result: ||C9 won · ✅ Correct||" in embed.fields[0].value
    assert "Result: ||KC won · ❌ Incorrect||" in embed.fields[1].value
    for index in (0, 1):
        assert "\n\n🏁 Past prediction\nYour pick:" in embed.fields[index].value
    assert "\n\n🔒 Voting closed\nYour pick: **C9**\n-# Awaiting result" in embed.fields[2].value
    assert "\n\n🗳️ Voting open\nYour pick:" in embed.fields[3].value
    assert embed.fields[3].value.endswith(
        "Your pick: **C9**\n-# (Voting closes in 2 hours and 1 minute)\n\n-# Use `/changevote` to change your prediction.")
    assert "🔵" not in embed.fields[3].value
    assert all(field.name != "\u200b" for field in embed.fields)
    cancelled = replace(data, matches=[replace(match, status=MatchStatus.CANCELLED)])
    assert "CANCELLED" in my_votes_embeds([(poll, match.team1_id)], cancelled)[0].fields[0].value
    assert "no longer valid" in my_votes_embeds([(poll, "OTHER")], data)[0].fields[0].value


def test_entire_history_is_paged_within_discord_embed_limits():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, True, None)
    embeds = my_votes_embeds([(poll, match.team1_id)] * 46, data)
    assert len(embeds) == 5
    assert sum(sum(field.name != "\u200b" for field in e.fields) for e in embeds) == 46
    assert all(len(e) <= 6000 and len(e.fields) <= 25 for e in embeds)


def test_unvoted_match_only_has_remaining_time_and_deadline_is_exact():
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    cutoff = match.start_time - timedelta(minutes=5)
    embed, = my_votes_embeds([(poll, None)], data, now=cutoff - timedelta(seconds=30))
    assert embed.fields[0].value == "Thu, 15 Oct 2026\n-# 11:00h (PT)\n\n🗳️ Voting open\n-# (Voting closes in less than a minute)"
    assert len(embed.fields) == 1
    closed, = my_votes_embeds([(poll, match.team1_id)], data, now=cutoff)
    assert "Awaiting result" in closed.fields[0].value
    assert "/changevote" not in closed.fields[0].value
    assert "Voting closes in" not in closed.fields[0].value


def test_upcoming_votes_only_command_hint_is_small_grey():
    from services.prediction_display import hidden_votes_payload
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    payload = hidden_votes_payload(match, now=match.start_time - timedelta(hours=2, minutes=20), stage_name="Play-In")
    text = payload["view"].children[0].children[0].content
    assert text.startswith("## __Votes: C9 vs KC__\n\nPlay-ins · Bo5\nThu, 15 Oct 2026\n\n")
    assert "\n\n**🗳️ Voting for this match closes in 2 hours and 15 minutes**\n\n" in text
    assert "**UTC:**" not in text
    assert [line for line in text.splitlines() if line.startswith("-# ")] == [
        "-# Use `/changevote` to make or change your prediction for these games"]
    assert "Votes are private until" not in text


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


def test_personal_history_uses_one_zone_and_currentvotes_omits_city_times(monkeypatch):
    from services.prediction_display import upcoming_votes_embed
    zones = ["Europe/London", "Europe/Berlin", "Asia/Tokyo"]
    monkeypatch.setattr(match_times, "get_registered_timezones", lambda: zones)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, True, None)
    history = my_votes_embeds([(poll, match.team1_id)], data, timezone_name="Europe/Berlin")[0].fields[0].value
    public_embed = upcoming_votes_embed(match, [], stage_name="Play-In")
    assert public_embed.description == "Play-ins · Bo5\nThu, 15 Oct 2026\n\nNo votes were made."
    assert all(field.inline for field in public_embed.fields[:2])
    assert public_embed.fields[2].inline is False
    public = public_embed.fields[2].value
    assert public == "🕒 UPCOMING"
    help_block = upcoming_votes_embed(match, []).footer.text
    assert all(line.startswith("Use /") for line in help_block.splitlines())
    assert "**" not in help_block
    assert history.startswith("Thu, 15 Oct 2026\n-# 20:00h (CEST)\n\n🔒 Voting closed\nYour pick:")
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
    history = my_votes_embeds([(poll, match.team1_id)], data, now=now, timezone_name=zone)[0].fields[0].value
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
    history = my_votes_embeds([(poll, match.team1_id)], data, now=now,
                             timezone_name="Europe/Berlin", time_format="12h")[0].fields[0].value
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
