from presentation_assertions import rendered_message, rendered_payload
import ast
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from models import Match, MatchStatus
from services.status_labels import MATCH_STATUS_LABELS
from services.reminder_engine import ReminderTracker
from services.reminder_scheduler import check_reminders
from services.tournament_data import load_tournament_data
from services.tournament_queries import get_matches_for_stage, get_standings
from services.standings_display import standings_text
from services.golgg import GOLGG_FOOTNOTES
from services.team_history import TeamHistoryView
from services.match_times import TOURNAMENT_TIMEZONE, format_clock_time, format_timezone_times, private_match_details
from services.presentation import embed_card_payload, message_payload
from services.prediction_display import VOTE_CONFIRMATION_HELP, group_voting_messages
from services.voting import personal_vote_payloads, schedule_vote_payloads, my_vote_payloads, current_votes_controls
from services.leaderboard_views import leaderboard_payload, leaderboard_context, show_personal_stats
from services.spoilers import current_policy, matchup, score_token


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_command(name, worlds, **dependencies):
    # Execute only the selected callback, never bot.py's startup or Discord registration.
    source = ast.parse((PROJECT_ROOT / "bot.py").read_text(encoding="utf-8"))
    callback = next(
        node for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    callback.decorator_list = []
    namespace = {
        "discord": SimpleNamespace(Interaction=object),
        "worlds": worlds,
        "MatchStatus": MatchStatus,
        "timedelta": timedelta,
        "ReminderTracker": ReminderTracker,
        "check_reminders": check_reminders,
        "get_all_user_reminder_preferences": lambda: [],
        "get_standings": get_standings,
        "standings_text": standings_text,
        "GOLGG_FOOTNOTES": GOLGG_FOOTNOTES,
        "TeamHistoryView": TeamHistoryView,
        "team_stats_text": AsyncMock(return_value="### __Worlds Stats__\nNot enough data"),
        "get_registered_timezones": lambda: [],
        "get_user_timezone": lambda user_id: None,
        "get_user_time_format": lambda user_id: "24h",
        "has_match_votes": lambda *args: True,
        "private_match_details": private_match_details,
        "TOURNAMENT_TIMEZONE": TOURNAMENT_TIMEZONE,
        "message_payload": message_payload,
        "group_voting_messages": group_voting_messages,
        "personal_vote_payloads": personal_vote_payloads,
        "current_votes_controls": current_votes_controls,
        "my_vote_payloads": my_vote_payloads,
        "schedule_vote_payloads": schedule_vote_payloads,
        "VOTE_CONFIRMATION_HELP": VOTE_CONFIRMATION_HELP,
        "leaderboard_payload": leaderboard_payload,
        "leaderboard_context": leaderboard_context,
        "show_personal_stats": show_personal_stats,
        "current_policy": current_policy,
        "matchup": matchup,
        "score_token": score_token,
        "embed_card_payload": embed_card_payload,
    }
    namespace.update(dependencies)
    exec(compile(ast.Module(body=[callback], type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace[name]


def run_command(name, worlds):
    send_message = AsyncMock()
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    asyncio.run(load_command(name, worlds)(interaction))
    send_message.assert_awaited_once()
    return send_message.call_args


@pytest.mark.parametrize("post", [None, ("2026-10-18", 10, 20)])
def test_dailypost_links_saved_board_privately_without_publishing(post):
    import discord

    worlds = SimpleNamespace(tournament=SimpleNamespace(id="worlds"))
    lookup_calls = []

    def lookup(guild_id, tournament_id):
        lookup_calls.append((guild_id, tournament_id))
        return post

    interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    callback = load_command("dailypost", worlds, asyncio=asyncio, discord=discord, get_latest_daily_post=lookup)
    asyncio.run(callback(interaction))

    assert lookup_calls == [(123, "worlds")]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    call = interaction.followup.send.call_args
    assert call.kwargs["ephemeral"] is True
    text = rendered_message(call).description
    if post:
        assert "https://discord.com/channels/123/10/20" in text
        assert "2026-10-18" in text
    else:
        assert "No daily voting post has been published" in text


def test_simulation_uses_earliest_scheduled_match_from_current_dataset():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    earliest = min(worlds.matches, key=lambda match: match.start_time)
    cancelled = replace(
        earliest, id="cancelled", status=MatchStatus.CANCELLED,
        start_time=earliest.start_time - timedelta(hours=1),
    )
    worlds = replace(worlds, matches=[*reversed(worlds.matches), cancelled])

    call = run_command("simulate_reminder", worlds)

    team1 = worlds.teams_by_id[earliest.team1_id].short_name
    team2 = worlds.teams_by_id[earliest.team2_id].short_name
    assert f"**{team1}** vs **{team2}** starts in 5 minutes!" in rendered_message(call).description
    assert "10:55h (PT)" in rendered_message(call).description
    assert call.kwargs["ephemeral"] is True


def test_simulation_without_scheduled_matches_is_ephemeral():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    worlds = replace(worlds, matches=[replace(m, status=MatchStatus.COMPLETED) for m in worlds.matches])

    call = run_command("simulate_reminder", worlds)

    assert "No scheduled matches" in rendered_message(call).description
    assert call.kwargs["ephemeral"] is True


def test_standings_uses_current_tournament_year():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2025.json")
    worlds = replace(worlds, tournament=replace(worlds.tournament, year=2026))

    call = run_command("standings", worlds)

    assert rendered_message(call).title == "__Worlds 2026: Swiss Standings__"


def test_standings_explains_missing_team_data_and_team_selection():
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    call = run_command("standings", data)
    text = rendered_message(call).description
    assert "all Swiss teams' series wins and losses" in text
    assert "Not enough data yet" in text
    assert "`/team`" in text and "`/leaderboard`" in text
    assert "No standings are available" not in text


def test_sample_standings_are_private_and_leave_real_records_unchanged():
    from copy import deepcopy
    import discord
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    original = deepcopy(data)
    callback = load_command("teststandings", data, discord=discord)
    reply = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(callback(reply))
    call = reply.response.send_message.call_args
    content = rendered_message(call)
    assert content.title.startswith("__TEST:")
    assert "Sample teams and records only" in content.description
    assert content.description.count(" · 2-0") == 4
    assert content.description.count(" · 1-1") == 8
    assert content.description.count(" · 0-2") == 4
    assert call.kwargs["ephemeral"] is True
    assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert data == original


def test_team_autocomplete_lists_known_teams_and_filters_by_name():
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    import discord
    callback = load_command("team_autocomplete", data, discord=discord,
                            app_commands=discord.app_commands, TBD_TEAM_ID="TBD")
    all_teams = asyncio.run(callback(None, ""))
    assert {item.value for item in all_teams} == {team.id for team in data.teams if team.id != "TBD"}
    assert all(item.value != "TBD" for item in all_teams)
    assert [item.value for item in asyncio.run(callback(None, "cloud"))] == ["C9"]
    assert [item.value for item in asyncio.run(callback(None, "C9"))] == ["C9"]


def test_player_autocomplete_is_alphabetical_filters_names_and_respects_discord_limit(monkeypatch):
    import discord
    from services.golgg_players import player_catalog
    from services import golgg_players
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    monkeypatch.setattr(golgg_players, "_catalogs", {2026: {str(i): f"Player {i:02}" for i in reversed(range(40))}})
    callback = load_command("player_autocomplete", data, player_catalog=player_catalog,
                            discord=discord, app_commands=discord.app_commands)
    choices = asyncio.run(callback(None, ""))
    assert len(choices) == 25
    assert [choice.name for choice in choices] == sorted(choice.name for choice in choices)
    assert [choice.name for choice in asyncio.run(callback(None, "PLAYER 39"))] == ["Player 39"]


def test_player_command_uses_selected_player_id_and_requires_name():
    import discord
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    build = AsyncMock(return_value="## Worlds Players\nNames")
    callback = load_command("player", data, discord=discord, player_reply=build)
    reply = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    with pytest.raises(TypeError):
        callback(reply)
    asyncio.run(callback(reply, "123"))
    build.assert_awaited_with("123", 2026)


def load_formatters(worlds):
    source = ast.parse((PROJECT_ROOT / "bot.py").read_text(encoding="utf-8"))
    names = {
        "local_datetime", "format_local_date", "format_local_time",
        "labels_are_duplicate", "display_stage_name", "format_relative_time", "format_match", "format_next_match", "format_team_matches", "format_recent_team_matches",
    }
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "worlds": worlds, "datetime": datetime, "UTC": UTC, "MATCH_STATUS_LABELS": MATCH_STATUS_LABELS,
        "ZoneInfo": ZoneInfo, "MatchStatus": MatchStatus, "ceil": ceil,
        "format_timezone_times": format_timezone_times,
        "format_clock_time": format_clock_time,
        "TOURNAMENT_TIMEZONE": TOURNAMENT_TIMEZONE,
        "current_policy": current_policy, "matchup": matchup, "score_token": score_token,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


def test_team_preview_contains_three_sample_states_without_changing_data():
    from copy import deepcopy
    import discord
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    original = deepcopy(data)
    callback = load_command("testteam", data, discord=discord, Match=Match,
                            datetime=datetime, UTC=UTC, format_team_matches=load_formatters(data)["format_team_matches"])
    reply = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(callback(reply, "AL"))
    call = reply.response.send_message.call_args
    rendered = rendered_message(call)
    assert rendered.title.startswith("__TEST:")
    assert "Sample matches and results only" in rendered.description
    for status in (MatchStatus.COMPLETED, MatchStatus.LIVE, MatchStatus.SCHEDULED):
        assert MATCH_STATUS_LABELS[status] in rendered.description
    assert "||" in rendered.description
    assert call.kwargs["ephemeral"] is True
    assert data == original


def test_team_without_confirmed_matches_explains_empty_schedule():
    import discord
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    send = AsyncMock()
    callback = load_command("team", data, discord=discord, get_matches_for_team=lambda *_: [],
                            send_long_message=send)
    asyncio.run(callback(SimpleNamespace(response=SimpleNamespace(defer=AsyncMock())), "AL"))
    assert "No confirmed matchups" in send.call_args.args[2][0]
    assert "Worlds Stats" in send.call_args.args[1]
    assert send.call_args.kwargs["deferred"] is True
    assert send.call_args.kwargs["footer"] == GOLGG_FOOTNOTES


def test_team_history_groups_by_stage_and_keeps_dates_as_small_match_details():
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    first = data.matches[0]
    swiss = next(stage for stage in data.stages if "swiss" in stage.name.lower())
    matches = [replace(first, id="swiss", stage_id=swiss.id, start_time=first.start_time - timedelta(days=1)),
               replace(first, id="later", start_time=first.start_time + timedelta(days=2)), first]
    lines = load_formatters(data)["format_team_matches"](matches, ["Europe/Berlin"])
    headings = [line for line in lines if line.startswith("## __")]
    assert headings == ["## __Play-ins__", "## __Swiss Stage__"]
    assert lines[1].index("## C9 vs KC") < lines[1].index("-# 15 October 2026")
    assert "17 October 2026" in lines[2]
    assert "14 October 2026" in lines[4]
    assert all("Play-ins ·" not in line for line in lines[1:])


def test_team_default_history_has_only_five_latest_finished_matches_and_inline_format():
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    first = data.matches[0]
    matches = [replace(first, id=f"done-{i}", status=MatchStatus.COMPLETED,
                       start_time=first.start_time - timedelta(days=i), winner_id=first.team1_id,
                       team1_score=3, team2_score=0) for i in range(7)]
    matches.append(replace(first, id="future", status=MatchStatus.SCHEDULED,
                           start_time=first.start_time + timedelta(days=1)))
    lines = load_formatters(data)["format_recent_team_matches"](matches, ["Europe/Berlin"])
    assert lines[0] == "## __Last 5 Matches__" and len(lines) == 6
    assert "15 October 2026 · Bo5" in lines[1]
    assert "11 October 2026 · Bo5" in lines[-1]
    assert all("\n-# Bo5\n" not in line for line in lines)
    assert all("UPCOMING" not in line for line in lines)


def test_golgg_footnotes_are_below_match_content_and_keep_muted_bullet_style():
    data = load_tournament_data(PROJECT_ROOT / "data/worlds_2026.json")
    callback = load_command("send_long_message", data)
    reply = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                            followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(callback(reply, "## Team\n\nWorlds Stats", ["Upcoming matches here"], footer=GOLGG_FOOTNOTES))
    text = rendered_message(reply.response.send_message.call_args).description
    assert text.index("Upcoming matches here") < text.index("Source: gol.gg")
    assert text.endswith("-# • *Source: gol.gg*\n-# • *Play-In and Main Event stats are shown separately.*")


@pytest.mark.parametrize("seconds, expected", [
    (7260, "in 2 hours 1 minute"),
    (3600, "in 1 hour"),
    (120, "in 2 minutes"),
    (61, "in 2 minutes"),
    (60, "in 1 minute"),
    (30, "in less than a minute"),
    (0, ""),
    (-60, ""),
])
def test_relative_time_uses_hours_and_minutes_without_seconds(seconds, expected):
    formatter = load_formatters(None)["format_relative_time"]
    now = datetime(2026, 10, 15, tzinfo=UTC)
    assert formatter(now + timedelta(seconds=seconds), now) == expected


@pytest.mark.parametrize("status", list(MatchStatus))
def test_match_countdown_is_only_shown_for_upcoming_scheduled_matches(status):
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = replace(worlds.matches[0], status=status)
    formatter = load_formatters(worlds)["format_match"]
    now = match.start_time - timedelta(hours=2, minutes=15)

    result = formatter(match, "Europe/Berlin", now=now)

    if status == MatchStatus.SCHEDULED:
        assert "**Berlin:** 20:00" in result
        assert "-# (in 2 hours 15 minutes)" in result
        assert "**20:00" not in result
        assert "(in " not in formatter(match, "Europe/Berlin", now=match.start_time)
    else:
        assert "(in " not in result


def schedule_callback(worlds, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    send_long = AsyncMock()
    dependencies = load_formatters(worlds)
    dependencies.update(
        datetime=Clock,
        get_registered_timezones=lambda: ["Europe/Berlin"],
        require_timezone=AsyncMock(return_value="Europe/Berlin"),
        get_matches_for_stage=get_matches_for_stage,
        send_long_message=send_long,
    )
    return load_command("schedule", worlds, **{k: v for k, v in dependencies.items() if k != "worlds"}), send_long


def test_full_schedule_header_and_local_date_groups_are_underlined():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    callback, send_long = schedule_callback(worlds, datetime(2026, 10, 14, tzinfo=UTC))
    asyncio.run(callback(SimpleNamespace(), "worlds-2026-play-in"))
    header, lines = send_long.call_args.args[1:]
    assert header == "# __2026 Worlds Schedule: Play-Ins Stage__"
    assert "## __15 October 2026__" in lines
    assert "## __16 October 2026__" not in lines
    assert lines[1].index("## C9 vs KC") < lines[1].index("20:00")


def test_next_five_schedule_filters_and_sorts_across_stages():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    now = datetime(2026, 10, 14, tzinfo=UTC)
    template = worlds.matches[0]
    upcoming = [replace(template, id=f"future-{i}", start_time=now + timedelta(hours=i),
                        stage_id=worlds.stages[i % 2].id) for i in range(1, 7)]
    excluded = [replace(template, id=f"excluded-{status}", status=status,
                        start_time=now + timedelta(minutes=1)) for status in MatchStatus
                if status != MatchStatus.SCHEDULED]
    past = replace(template, id="past", start_time=now - timedelta(hours=1))
    worlds = replace(worlds, matches=[*reversed(upcoming), *excluded, past])
    callback, send_long = schedule_callback(worlds, now)
    asyncio.run(callback(SimpleNamespace(), "next-five"))
    header, lines = send_long.call_args.args[1:]
    assert header == "# __2026 Worlds Schedule: Next 5 Games__"
    match_lines = [line for line in lines if "vs" in line]
    assert len(match_lines) == 5
    for i, line in enumerate(match_lines, start=1):
        assert f"(in {i} hour{'s' if i != 1 else ''})" in line


def test_next_five_schedule_handles_no_upcoming_matches():
    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    callback, send_long = schedule_callback(worlds, datetime(2027, 1, 1, tzinfo=UTC))
    send_message = AsyncMock()
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    asyncio.run(callback(interaction, "next-five"))
    send_long.assert_not_awaited()
    assert rendered_message(send_message.call_args).description == "No upcoming scheduled matches found."


def test_schedule_and_next_show_all_setup_zones_with_only_one_countdown():
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    zones = ["Europe/London", "Europe/Berlin", "Asia/Tokyo"]
    match = data.matches[0]
    now = match.start_time - timedelta(hours=2)
    callback, send_long = schedule_callback(data, now)
    callback.__globals__["get_registered_timezones"] = lambda: zones
    asyncio.run(callback(SimpleNamespace(), "next-five"))
    match_text = next(line for line in send_long.call_args.args[2] if "## C9 vs KC" in line)
    assert "London" in match_text and "19:00" in match_text
    assert "Berlin" in match_text and "20:00" in match_text
    assert "**Tokyo:** 3:00h (Fri, 16 Oct 2026)" in match_text
    assert match_text.count("(in ") == 1
    assert match_text.index("(in ") < match_text.index("**London:**")
    assert "**London:** 19:00" in match_text and "**19:00" not in match_text
    next_text = load_formatters(data)["format_next_match"](match, "Europe/Berlin", now, zones)
    assert all(line in next_text for line in format_timezone_times(match.start_time, zones).splitlines())
    assert next_text.count("(in ") == 1
    assert next_text.index("(in ") < next_text.index("**London:**")


def test_schedule_registers_a_required_dropdown_without_starting_discord(monkeypatch):
    import runpy
    import discord
    import dotenv
    from services import delivery_records, live_tournament, polls, server_settings, user_settings

    worlds = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    monkeypatch.setenv("DISCORD_TOKEN", "offline-test-token")
    monkeypatch.setenv("GUILD_ID", "123")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    monkeypatch.setattr(user_settings, "initialize_database", lambda: None)
    monkeypatch.setattr(user_settings, "get_notification_settings", lambda user_id: (True, True, 30))
    timezones = {123: "Europe/Berlin"}
    monkeypatch.setattr(user_settings, "get_user_timezone", lambda user_id: timezones.get(user_id))
    monkeypatch.setattr(user_settings, "set_user_timezone", lambda user_id, zone: timezones.update({user_id: zone}))
    formats = {}
    monkeypatch.setattr(user_settings, "get_user_time_format", lambda user_id: formats.get(user_id, "24h"))
    monkeypatch.setattr(user_settings, "set_user_time_format", lambda user_id, value: formats.update({user_id: value}))
    followed = {123: {"C9"}}
    monkeypatch.setattr(user_settings, "get_followed_team_ids", lambda user_id: followed.get(user_id, set()))
    monkeypatch.setattr(user_settings, "set_followed_team_ids", lambda user_id, teams: followed.update({user_id: teams}))
    monkeypatch.setattr(user_settings, "get_all_games_server_ping", lambda user_id: False)
    monkeypatch.setattr(server_settings, "initialize_server_settings", lambda: None)
    monkeypatch.setattr(delivery_records, "initialize_delivery_records", lambda: None)
    monkeypatch.setattr(polls, "initialize_polls", lambda: None)
    monkeypatch.setattr(live_tournament, "LiveTournamentStore", lambda *args: SimpleNamespace(data=worlds))
    monkeypatch.setattr(discord.Client, "run", lambda *args, **kwargs: None)
    namespace = runpy.run_path(str(PROJECT_ROOT / "bot.py"))
    command = namespace["bot"].tree.get_command("schedule")
    parameter, = command.parameters
    assert parameter.name == "view"
    assert parameter.required is True
    assert [choice.name for choice in parameter.choices] == [
        "Next 5 Games", "Play-ins", "Swiss Stage", "Quarterfinals", "Semifinals", "Final",
    ]
    server_command = namespace["bot"].tree.get_command("serversetup")
    assert namespace["bot"].tree.get_command("pickems").description == "Open the server's official Riot Pick'ems leaderboard."
    assert namespace["bot"].tree.get_command("leaderboard").guild_only is True
    assert namespace["bot"].tree.get_command("myvotestats").guild_only is True
    player_option, = namespace["bot"].tree.get_command("player").parameters
    assert player_option.name == "name" and player_option.required and player_option.autocomplete
    assert namespace["bot"].tree.get_command("myvotes").guild_only is True
    assert namespace["bot"].tree.get_command("changevote").guild_only is True
    assert namespace["bot"].tree.get_command("today") is not None
    assert namespace["bot"].tree.get_command("tomorrow") is not None
    assert namespace["bot"].tree.get_command("previous") is not None
    assert not namespace["bot"].tree.get_command("myvotes").checks
    assert server_command.guild_only is True
    assert server_command.default_permissions.manage_guild
    interaction = SimpleNamespace(permissions=discord.Permissions.none())
    with pytest.raises(discord.app_commands.MissingPermissions):
        server_command.checks[0](interaction)
    interaction.permissions = discord.Permissions(manage_guild=True)
    assert server_command.checks[0](interaction)
    async def check_help():
        help_command = namespace["bot"].tree.get_command("help")
        category_option, = help_command.parameters
        assert category_option.name == "category"
        assert category_option.required is False
        categories = ["Setup", "Voting", "Leaderboards", "Match and Player Infos"]
        assert [choice.name for choice in category_option.choices] == categories
        reply = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await namespace["bot"].tree.get_command("help").callback(reply)
        call = reply.response.send_message.call_args
        text = rendered_message(call).description
        assert rendered_message(call).title == "__Worlds Bot: List of all Commands__"
        headings = [f"## __{index}. {category}__" for index, category in enumerate(
            ("Setup", "Voting", "Leaderboards", "Match and Player Infos"), 1)]
        assert [line for line in text.splitlines() if line.startswith("## __")] == headings
        for item in namespace["bot"].tree.get_commands():
            if item.name in {"serversetup", "help", "ping"}:
                assert f"`/{item.name}`" not in text
            elif item.name in {"votehistory", "schedule", "team", "player"}:
                parameter, = item.parameters
                assert parameter.required
                assert f"`/{item.name} <" in text
            elif not item.name.startswith(("test", "simulate")):
                assert f"`/{item.name}`" in text
            assert 1 <= len(item.description) <= 100
        assert call.kwargs["ephemeral"] is True
        assert len(text) < 4000
        for index, category in enumerate(categories, 1):
            reply.response.send_message.reset_mock()
            await help_command.callback(reply, category=index)
            section_call = reply.response.send_message.call_args
            section_text = rendered_message(section_call).description
            assert [line for line in section_text.splitlines() if line.startswith("## __")] == [
                f"## __{index}. {category}__",
            ]
            expected_section = text.split(f"## __{index}. {category}__", 1)[1].split("\n\n## __", 1)[0]
            assert expected_section.strip() in section_text
            assert section_call.kwargs["ephemeral"] is True
    asyncio.run(check_help())
    content = namespace["build_setup_content"](123, "Test user")
    assert "DM" not in content
    assert "Europe/Berlin" in content
    assert "C9" in content
    async def check_setup():
        view = namespace["SetupView"](123)
        assert len(view.children) == 5
        assert all("DM" not in item.label for item in view.children if isinstance(item, discord.ui.Button))
        card, = message_payload(content, controls=view)["view"].children
        for label, picker in (("Timezone:", namespace["SetupTimezoneSelect"]),
                              ("Time Format:", namespace["TimeFormatSelect"]),
                              ("Teams You Are Following:", namespace["TeamSelect"])):
            position = next(i for i, item in enumerate(card.children)
                            if isinstance(item, discord.ui.TextDisplay) and label in item.content)
            assert isinstance(card.children[position + 1], discord.ui.ActionRow)
            assert isinstance(card.children[position + 1].children[0], picker)
            assert f"__{label}__" in card.children[position].content
        spacers = [item for item in card.children if isinstance(item, discord.ui.Separator)]
        assert len(spacers) == 4
        assert "Daily Worlds Posts" not in str(message_payload(content, controls=view))
        assert view.toggle_all_games_ping.disabled
        from services import worlds_role
        role = SimpleNamespace(id=789, mention="<@&789>")
        monkeypatch.setattr(worlds_role, "get_server_settings", lambda _: SimpleNamespace(worlds_role_id=789))
        guild = SimpleNamespace(id=123, get_role=lambda _: role)
        member = SimpleNamespace(roles=[role])
        subscribed = namespace["SetupView"](123, member=member, guild=guild)
        assert not subscribed.toggle_all_games_ping.disabled
        assert "OFF (Currently ON)" in subscribed.toggle_all_games_ping.label
        subscribed_text = namespace["build_setup_content"](123, "Test user", member=member, guild=guild)
        assert "**On** *(Daily posts and 5-minute match reminders)*" in subscribed_text
        member.roles = []
        unsubscribed = namespace["SetupView"](123, member=member, guild=guild)
        assert "ON (Currently OFF)" in unsubscribed.toggle_all_games_ping.label
        assert "Daily Worlds Posts" not in subscribed_text
        assert all(not item.visible and item.spacing == discord.SeparatorSpacing.large for item in spacers)
        select = next(item for item in card.walk_children() if isinstance(item, namespace["TimeFormatSelect"]))
        assert [option.value for option in select.options] == ["24h", "12h"]
        assert select.options[0].default is True
        select._values = ["12h"]
        reply = SimpleNamespace(user=SimpleNamespace(id=123, display_name="Test user"),
                                response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await select.callback(reply)
        assert formats[123] == "12h"
        text = rendered_message(reply.response.edit_message.call_args).description
        assert "1:00 am (CEST)" in text
        other = SimpleNamespace(user=SimpleNamespace(id=456), response=SimpleNamespace(send_message=AsyncMock()))
        await select.callback(other)
        assert 456 not in formats
        assert other.response.send_message.call_args.kwargs["ephemeral"] is True
        teams = next(item for item in card.walk_children() if isinstance(item, namespace["TeamSelect"]))
        assert {option.value for option in teams.options if option.default} == {"C9"}
        assert not any(item.label == "Change Followed Teams" for item in view.children if isinstance(item, discord.ui.Button))
        teams._values = ["T1", "FUR"]
        await teams.callback(reply)
        assert followed[123] == {"T1", "FUR"}
        updated = reply.response.edit_message.call_args.kwargs["view"]
        updated_teams = next(item for item in updated.walk_children() if isinstance(item, namespace["TeamSelect"]))
        assert {option.value for option in updated_teams.options if option.default} == {"T1", "FUR"}
        teams._values = []
        await teams.callback(reply)
        assert followed[123] == set()
        await teams.callback(other)
        assert 456 not in followed
        timezone = next(item for item in card.walk_children() if isinstance(item, namespace["SetupTimezoneSelect"]))
        timezone._values = ["Europe"]
        await timezone.callback(reply)
        updated = reply.response.edit_message.call_args.kwargs["view"]
        cities = next(item for item in updated.walk_children() if isinstance(item, namespace["SetupTimezoneSelect"]))
        assert cities.region == "Europe"
        cities._values = ["Europe/London"]
        await cities.callback(reply)
        assert timezones[123] == "Europe/London"
        assert "Europe/London" in rendered_message(reply.response.edit_message.call_args).description
        await cities.callback(other)
        assert 456 not in timezones
        view.stop()
    asyncio.run(check_setup())


def test_prediction_leaderboard_shows_tied_ranks_without_exposing_vote_counts():
    import discord
    from services.prediction_stats import PredictionStats
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    rows = [PredictionStats(1, 2, 3, 10, 2, 2), PredictionStats(2, 2, 3, 10, 2, 2), PredictionStats(3, 1, 4, 10, 0, 1)]
    members = {user_id: SimpleNamespace(display_name=f"Friend {user_id}") for user_id in (1, 2, 3)}
    send = AsyncMock()
    interaction = SimpleNamespace(
        guild_id=123, guild=SimpleNamespace(get_member=members.get),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=send),
    )
    context = AsyncMock(return_value=(None, [(row, members[row.user_id].display_name) for row in rows], None))
    callback = load_command("leaderboard", data, leaderboard_context=context, discord=discord)
    asyncio.run(callback(interaction))
    text = rendered_message(send.call_args).description
    assert "### `1.` Friend 1: 2 correct" in text
    assert "### `1.` Friend 2: 2 correct" in text
    assert "### `3.` Friend 3: 1 correct" in text
    assert "behind" not in text and "Level on" not in text
    assert "66.7% correct" in text and "Voted on 3 of 10 past matches" in text
    assert "Current streak: 2*\n-# *Best streak: 2" in text
    assert rendered_message(send.call_args).title == "__Communism Worlds Leaderboard__"
    assert "ephemeral" not in send.call_args.kwargs
    assert send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_pickems_shows_configured_link_and_has_exact_description():
    import discord
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    send = AsyncMock()
    interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(send_message=send))
    callback = load_command(
        "pickems", data, discord=discord,
        get_server_settings=lambda *args: SimpleNamespace(pickems_url="https://pickem.lolesports.com/example-group"),
    )
    asyncio.run(callback(interaction))
    assert "https://pickem.lolesports.com/example-group" in rendered_message(send.call_args).description


def test_myvotes_only_queries_caller_and_all_messages_are_ephemeral():
    import discord
    from unittest.mock import Mock
    from services.prediction_display import my_votes_embeds
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    lookup = Mock(return_value=[])
    interaction = SimpleNamespace(
        guild_id=123, user=SimpleNamespace(id=7, display_name="lua"),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )
    callback = load_command("myvotes", data, asyncio=asyncio, discord=discord,
                            get_user_votes=lookup, my_votes_embeds=my_votes_embeds)
    asyncio.run(callback(interaction))
    lookup.assert_called_once_with(123, data.tournament.id, 7, include_open=True)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    assert "haven't made" in rendered_message(interaction.followup.send.call_args).description
    assert "@lua's Worlds Predictions" in rendered_message(interaction.followup.send.call_args).title


def test_myvotes_shows_all_active_test_picks_then_returns_to_real_worlds():
    import discord
    from unittest.mock import Mock
    from services.polls import Poll
    from services.prediction_display import my_votes_embeds

    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    now = datetime(2026, 10, 16, 16, 40, tzinfo=UTC)
    token = "prediction-test:myvotes"
    matches = [replace(match, id=f"{token}:{index}", tournament_id=token,
                       start_time=now + timedelta(minutes=6 + index), status=MatchStatus.SCHEDULED)
               for index, match in enumerate(data.matches[:2])]
    test_data = replace(data, tournament=replace(data.tournament, id=token), matches=matches)
    rows = [(Poll(123, match.id, token, match.team1_id, match.team2_id,
                  match.start_time, False, None), match.team2_id) for match in matches]
    lookup = Mock(side_effect=[rows, []])
    client = SimpleNamespace(prediction_test_manager=SimpleNamespace(
        store=SimpleNamespace(data=test_data), clock=lambda: now,
        personal_vote_targets=AsyncMock(return_value=[poll for poll, _ in rows])))
    interaction = SimpleNamespace(
        client=client, guild_id=123, user=SimpleNamespace(id=7, display_name="lua"),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )
    callback = load_command("myvotes", data, asyncio=asyncio, discord=discord,
                            get_user_votes=lookup, my_votes_embeds=my_votes_embeds)
    asyncio.run(callback(interaction))
    lookup.assert_called_with(123, token, 7, include_open=True, now=now)
    rendered = rendered_message(interaction.followup.send.call_args)
    assert "TEST:" in rendered.title
    assert sum(isinstance(item, discord.ui.Button) for item in interaction.followup.send.call_args.kwargs["view"].walk_children()) == 4
    assert rendered.description.count("Your pick:") == 2
    for match in matches:
        assert f"Your pick: **{match.team2_id}**" in rendered.description
        assert f"**{match.team1_id}** vs **{match.team2_id}**" in rendered.description
    assert "Voting closes in 1 minute" in rendered.description
    assert "Voting closes in 2 minutes" in rendered.description

    client.prediction_test_manager = None
    asyncio.run(callback(interaction))
    lookup.assert_called_with(123, data.tournament.id, 7, include_open=True)
    assert "TEST:" not in rendered_message(interaction.followup.send.call_args).title
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs["ephemeral"] is True
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.parametrize("has_upcoming", [True, False])
def test_votes_only_shows_earliest_upcoming_scheduled_match(has_upcoming):
    import discord
    from unittest.mock import Mock
    from services.prediction_display import upcoming_votes_embed
    from services.tournament_queries import get_matches_for_votes
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    now = datetime(2026, 10, 14, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    first = data.matches[0]
    eligible = [replace(first, id="later", start_time=now + timedelta(hours=2)),
                replace(first, id="next", start_time=now + timedelta(hours=1))] if has_upcoming else []
    excluded = [replace(first, id=status.value, status=status, start_time=now + timedelta(minutes=1))
                for status in MatchStatus if status not in {MatchStatus.SCHEDULED, MatchStatus.LIVE}]
    data = replace(data, matches=eligible + excluded + [replace(first, id="past", start_time=now - timedelta(hours=1))])
    lookup = Mock(return_value=[(7, first.team1_id, "Lua")])
    interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    callback = load_command("currentvotes", data, asyncio=asyncio, datetime=Clock, UTC=UTC, discord=discord,
                            get_upcoming_match_votes=lookup, upcoming_votes_embed=upcoming_votes_embed,
                            get_recent_completed_match_ids=lambda *args: set(), get_matches_for_votes=get_matches_for_votes)
    asyncio.run(callback(interaction))
    sent = interaction.followup.send.call_args
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    if has_upcoming:
        lookup.assert_called_once_with(123, "next")
        assert "@Lua" in rendered_message(sent).fields[0].value
        assert "voting closed" not in rendered_message(sent).description.lower()
    else:
        lookup.assert_not_called()
        assert "No upcoming" in rendered_message(sent).description


@pytest.mark.parametrize("has_results", [False, True])
def test_currentvotes_groups_live_and_upcoming_match_in_one_message(has_results):
    import discord
    from services.prediction_display import hidden_votes_payload, upcoming_votes_embed
    from services.tournament_queries import get_matches_for_votes
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    first, second = data.matches[:2]
    first = replace(first, status=MatchStatus.LIVE)
    data = replace(data, matches=[first, second])
    now = first.start_time + timedelta(hours=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    def lookup(guild_id, match_id):
        if match_id == first.id and has_results:
            return [(7, first.team1_id, "Lua")]
        raise ValueError("Results hidden")
    callback = load_command("currentvotes", data, asyncio=asyncio, datetime=Clock, UTC=UTC, discord=discord,
                            get_recent_completed_match_ids=lambda *args: set(),
                            get_matches_for_votes=get_matches_for_votes, get_upcoming_match_votes=lookup,
                            hidden_votes_payload=hidden_votes_payload, upcoming_votes_embed=upcoming_votes_embed,
                            has_match_votes=lambda *args: has_results)
    interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(callback(interaction))
    interaction.followup.send.assert_awaited_once()
    payload = interaction.followup.send.call_args.kwargs
    assert payload["allowed_mentions"].to_dict() == {"parse": []}
    if has_results:
        live, upcoming = payload["embeds"]
        assert live.title is None and upcoming.title is None
        assert "**C9** vs **KC**" in live.description and "**FUR** vs **MVK**" in upcoming.description
        assert all(field.inline for field in live.fields[:2])
        assert "100%" in live.fields[0].value and "@Lua" in live.fields[0].value
        assert "Voting for this match closes in" in upcoming.description
        assert "%" not in upcoming.description and "Lua" not in upcoming.description
    else:
        cards = payload["view"].children
        assert len(cards) == 2
        assert "**C9** vs **KC**" in cards[0].children[0].content
        assert "Voting closed" in cards[0].children[0].content
        assert "No votes were made." in cards[0].children[0].content
        assert "**FUR** vs **MVK**" in cards[1].children[0].content
        assert "Voting for this match closes in" in cards[1].children[0].content
        assert "No votes were made." not in cards[1].children[0].content


def test_currentvotes_keeps_public_vote_buttons_when_also_showing_results():
    import discord
    from services.polls import Poll
    from services.prediction_display import hidden_votes_payload, upcoming_votes_embed
    from services.tournament_queries import get_matches_for_votes
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    first, second = data.matches[:2]
    first = replace(first, status=MatchStatus.LIVE)
    data = replace(data, matches=[first, second])
    now = first.start_time + timedelta(hours=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    poll = Poll(123, second.id, data.tournament.id, second.team1_id, second.team2_id,
                second.start_time, False, None)
    manager = SimpleNamespace(personal_vote_targets=AsyncMock(return_value=[poll]))
    def lookup(guild_id, match_id):
        if match_id == first.id:
            return [(7, first.team1_id, "Lua")]
        raise ValueError("Results hidden")
    callback = load_command("currentvotes", data, asyncio=asyncio, datetime=Clock, UTC=UTC, discord=discord,
                            get_recent_completed_match_ids=lambda *args: set(),
                            get_matches_for_votes=get_matches_for_votes, get_upcoming_match_votes=lookup,
                            hidden_votes_payload=hidden_votes_payload, upcoming_votes_embed=upcoming_votes_embed)
    interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7),
                                  client=SimpleNamespace(voting_manager=manager),
                                  response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(callback(interaction))
    calls = interaction.followup.send.call_args_list
    assert len(calls) == 2
    assert "@Lua" in calls[0].kwargs["embed"].fields[0].value
    card = calls[1].kwargs["view"].children[0]
    row = next(item for item in card.children if isinstance(item, discord.ui.ActionRow))
    assert [button.item.team_id for button in row.children] == [second.team1_id, second.team2_id]
    assert all(not button.item.disabled for button in row.children)
    assert all(button.item.custom_id.startswith("worldsday:123:") for button in row.children)
    assert card.children.index(row) < len(card.children) - 1
    assert "-#" in card.children[-1].content
    assert "@Lua" not in " ".join(item.content for item in card.walk_children()
                                    if isinstance(item, discord.ui.TextDisplay))


@pytest.mark.parametrize("command, offset", [("today", 0), ("tomorrow", 1)])
def test_quick_day_commands_delegate_to_shared_schedule(command, offset):
    show = AsyncMock()
    interaction = SimpleNamespace()
    asyncio.run(load_command(command, None, show_day_schedule=show)(interaction))
    show.assert_awaited_once_with(interaction, offset)


def day_callback(data, now, timezone="Europe/Berlin"):
    import discord
    from services.prediction_display import PREDICTION_BLUE
    from services.tournament_queries import get_matches_for_local_date
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    dependencies = {key: value for key, value in load_formatters(data).items() if key != "worlds"}
    dependencies.update(datetime=Clock, require_timezone=AsyncMock(return_value=timezone),
                        get_matches_for_local_date=get_matches_for_local_date,
                        get_registered_timezones=lambda: ["Europe/London", "Europe/Berlin", "Asia/Tokyo"],
                        discord=discord, PREDICTION_BLUE=PREDICTION_BLUE)
    return load_command("show_day_schedule", data, **dependencies)


@pytest.mark.parametrize("offset", [0, 1])
def test_daily_lookup_is_public_with_all_zones_and_no_vote_results(offset):
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    now = datetime(2026, 10, 15, 8, tzinfo=UTC)  # Oct 15 in Los Angeles.
    start = datetime(2026, 10, 15 + offset, 18, tzinfo=UTC)
    template = data.matches[0]
    matches = [replace(template, id=status.value, status=status,
                       start_time=start + timedelta(hours=i),
                       team1_score=3 if status == MatchStatus.COMPLETED else None,
                       team2_score=1 if status == MatchStatus.COMPLETED else None)
               for i, status in enumerate([MatchStatus.SCHEDULED, MatchStatus.LIVE, MatchStatus.COMPLETED])]
    data = replace(data, matches=list(reversed(matches)))
    send = AsyncMock()
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=send), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(day_callback(data, now)(interaction, offset))
    call = send.call_args
    assert call.kwargs["ephemeral"] is False
    assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    text = rendered_message(call).description
    assert f"{15 + offset} Oct 2026" in text
    assert all(city in text for city in ("London", "Berlin", "Tokyo"))
    assert "Play-ins" in text and "Opening Round" in text
    assert text.index("19:00") < text.index("21:00") < text.index("22:00")
    assert "UPCOMING" in text and "LIVE" in text and "FINISHED" in text
    assert "3–1" in text
    assert "Voting Results" not in text and "%" not in text
    if offset == 0:
        assert VOTE_CONFIRMATION_HELP in text


def test_daily_lookup_no_matches_is_public():
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    send = AsyncMock()
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=send))
    asyncio.run(day_callback(data, datetime(2027, 1, 1, tzinfo=UTC))(interaction, 0))
    assert "No Worlds matches" in rendered_message(send.call_args).description
    assert send.call_args.kwargs["ephemeral"] is False


def test_daily_lookup_long_schedule_splits_into_public_embeds():
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    start = datetime(2026, 10, 15, 10, tzinfo=UTC)
    data = replace(data, matches=[replace(data.matches[0], id=f"game-{i}", start_time=start + timedelta(minutes=10 * i)) for i in range(30)])
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(day_callback(data, start)(interaction, 0))
    calls = [interaction.response.send_message.call_args, *interaction.followup.send.call_args_list]
    assert len(calls) > 1
    assert all(call.kwargs["ephemeral"] is False and len(rendered_message(call).description) <= 4096 for call in calls)
    assert sum(rendered_message(call).description.count("**C9** vs **KC**") for call in calls) == 30


def test_daily_lookup_uses_official_day_without_requiring_user_setup():
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    callback = day_callback(data, datetime(2026, 10, 15, 1, tzinfo=UTC), timezone=None)
    prompt = AsyncMock(side_effect=AssertionError("Public Riot-day lookup must not require personal setup"))
    callback.__globals__["require_timezone"] = prompt
    send = AsyncMock()
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send))
    asyncio.run(callback(interaction, 0))
    send.assert_awaited_once()
    assert "14 Oct 2026" in rendered_message(send.call_args).description
    assert "Europe/Berlin" not in rendered_message(send.call_args).description
    assert send.call_args.kwargs["ephemeral"] is False
    prompt.assert_not_awaited()


def test_long_command_output_preserves_schedule_title_and_date_on_every_page():
    import discord
    callback = load_command("send_long_message", None, discord=discord)
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    header = "# __2026 Worlds Schedule: Swiss Stage__"
    date = "## __23 October 2026__"
    games = ["## C9 vs KC\n-# **Berlin:** 20:00\n" + "a" * 400 for _ in range(10)]
    asyncio.run(callback(interaction, header, [date, *games]))
    calls = [interaction.response.send_message.call_args, *interaction.followup.send.call_args_list]
    assert len(calls) > 1
    for call in calls:
        embed = rendered_message(call)
        assert embed.title == "__2026 Worlds Schedule: Swiss Stage__"
        assert date in embed.description
        assert len(embed.description) <= 4096


@pytest.mark.parametrize("open_poll", [True, False])
@pytest.mark.parametrize("already_voted", [True, False])
def test_vote_command_is_private_and_uses_existing_vote_controls(open_poll, already_voted):
    import discord
    from services.polls import Poll
    from services.voting import PersonalVoteView
    from services.prediction_display import change_vote_text
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = data.matches[0]
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    manager = SimpleNamespace(clock=lambda: match.start_time - timedelta(hours=2, minutes=20), store=SimpleNamespace(data=data), personal_vote_targets=AsyncMock(return_value=[poll] if open_poll else []))
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), client=SimpleNamespace(voting_manager=manager),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    dependencies = {key: value for key, value in load_formatters(data).items() if key != "worlds"}
    callback = load_command("changevote", data, **dependencies, PersonalVoteView=PersonalVoteView, discord=discord,
                            asyncio=asyncio, change_vote_text=change_vote_text, get_user_votes=lambda *args: [(poll, match.team1_id)] if already_voted else [])
    asyncio.run(callback(interaction))
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    call = interaction.followup.send.call_args
    assert call.kwargs["ephemeral"] is True
    assert rendered_message(call).title == "__Your Currently Active Predictions__"
    if open_poll:
        buttons = [item for item in call.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
        assert [button.team_id for button in buttons] == [match.team1_id, match.team2_id]
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert "-# *(Voting closes in 2 hours and 15 minutes)*" in rendered_message(call).description
        assert "UPCOMING" not in rendered_message(call).description
        if already_voted:
            assert "Change your vote here." in rendered_message(call).description
            assert "-# Change your vote here." not in rendered_message(call).description
            assert "-# 🗳️ Voting open" not in rendered_message(call).description
            assert buttons[0].style == discord.ButtonStyle.success
            assert buttons[1].style == discord.ButtonStyle.primary
            assert buttons[0].label == f"{match.team1_id} ✓"
            assert "✓" not in buttons[1].label
        else:
            assert "Your current prediction:" not in rendered_message(call).description
            assert all("✓" not in button.label for button in buttons)
            assert all(button.style == discord.ButtonStyle.primary for button in buttons)
    else:
        assert "Voting is closed" in rendered_message(call).description


def test_vote_shortcut_uses_active_test_instead_of_live_tournament():
    import discord
    from services.polls import Poll
    from services.voting import PersonalVoteView
    from services.prediction_display import change_vote_text
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = replace(data.matches[0], id="test:first", tournament_id="isolated-test")
    test_data = replace(data, matches=[match])
    poll = Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
    test_manager = SimpleNamespace(clock=lambda: match.start_time - timedelta(hours=2, minutes=20), test_mode=True, store=SimpleNamespace(data=test_data), personal_vote_targets=AsyncMock(return_value=[poll]))
    live_manager = SimpleNamespace(personal_vote_targets=AsyncMock(side_effect=AssertionError("Must not query live picks during active test")))
    interaction = SimpleNamespace(user=SimpleNamespace(id=7),
        client=SimpleNamespace(prediction_test_manager=test_manager, voting_manager=live_manager),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    dependencies = {key: value for key, value in load_formatters(data).items() if key != "worlds"}
    callback = load_command("changevote", data, **dependencies, PersonalVoteView=PersonalVoteView, discord=discord,
                            asyncio=asyncio, change_vote_text=change_vote_text, get_user_votes=lambda *args: [(poll, match.team2_id)])
    asyncio.run(callback(interaction))
    test_manager.personal_vote_targets.assert_awaited_once_with(7)
    live_manager.personal_vote_targets.assert_not_awaited()
    assert rendered_message(interaction.followup.send.call_args).title == "__TEST: Your Currently Active Predictions__"
    buttons = [item for item in interaction.followup.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
    assert all(button.poll.tournament_id == "isolated-test" for button in buttons)


def test_changevote_command_includes_every_open_match_in_one_private_response():
    import discord
    from services.polls import Poll
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    first, second = data.matches[:2]
    available = [Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
                 for match in (first, second)]
    manager = SimpleNamespace(store=SimpleNamespace(data=data), clock=lambda: first.start_time - timedelta(hours=1),
                              personal_vote_targets=AsyncMock(return_value=available))
    interaction = SimpleNamespace(user=SimpleNamespace(id=7), client=SimpleNamespace(voting_manager=manager),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    saved = [(available[0], first.team2_id), (available[1], second.team1_id)]
    callback = load_command("changevote", data, asyncio=asyncio, discord=discord, get_user_votes=lambda *args: saved)
    asyncio.run(callback(interaction))
    interaction.followup.send.assert_awaited_once()
    payload = interaction.followup.send.call_args.kwargs
    assert payload["ephemeral"] is True
    buttons = [item for item in payload["view"].walk_children() if isinstance(item, discord.ui.Button)]
    assert len(buttons) == 4
    assert [(button.poll.match_id, button.team_id) for button in buttons if button.style == discord.ButtonStyle.success] == [
        (first.id, first.team2_id), (second.id, second.team1_id)]
    assert payload["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.parametrize("count", [0, 1, 3])
def test_previous_is_public_and_does_not_leak_results_outside_spoilers(count):
    import re
    from services.tournament_queries import get_previous_matches
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    now = datetime(2026, 10, 17, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    matches = [replace(data.matches[0], id=f"done-{i}", status=MatchStatus.COMPLETED,
                       start_time=datetime(2026, 10, 15, 18, tzinfo=UTC) + timedelta(hours=i),
                       team1_score=3, team2_score=i, winner_id="C9") for i in range(count)]
    data = replace(data, matches=matches)
    dependencies = {key: value for key, value in load_formatters(data).items() if key != "worlds"}
    sender = load_command("send_long_message", data)
    callback = load_command("previous", data, **dependencies, get_previous_matches=get_previous_matches,
                            send_long_message=sender, get_registered_timezones=lambda: ["Europe/Berlin", "Asia/Seoul"])
    callback.__globals__["datetime"] = Clock
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(callback(interaction))
    call = interaction.response.send_message.call_args
    assert call.kwargs.get("ephemeral", False) is False
    card = rendered_message(call)
    assert card.title == "__Previous Worlds Matches__"
    if not count:
        assert "No completed" in card.description
        return
    assert card.description.count("**C9** vs **KC**") == min(count, 2)
    assert "**Berlin:**" in card.description
    assert "**Seoul:**" in card.description
    assert "-# **Berlin:**" not in card.description and "-# **Seoul:**" not in card.description
    assert "-# Play-ins" not in card.description
    assert card.description.count("🏁 FINISHED\n### Result: ||") == min(count, 2)
    assert "FINAL" not in card.description
    hidden = re.findall(r"\|\|(.*?)\|\|", card.description)
    assert len(hidden) == min(count, 2)
    visible = re.sub(r"\|\|.*?\|\|", "", card.description)
    assert "3–" not in visible and "Winner:" not in visible
    assert "UPCOMING" not in card.description
    assert "Click a spoiler" in card.description
    if count == 3:
        assert "3–0" not in card.description
        assert card.description.index("3–2") < card.description.index("3–1")


def test_previous_hides_winner_when_scores_are_not_available():
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = replace(data.matches[0], status=MatchStatus.COMPLETED, winner_id="KC", team1_score=None, team2_score=None)
    text = load_formatters(data)["format_match"](match, "Europe/Berlin", hide_result=True)
    assert "||Winner: KC||" in text
    assert "UPCOMING" not in text


@pytest.mark.parametrize("status", [MatchStatus.LIVE, MatchStatus.COMPLETED])
def test_all_schedule_scores_are_spoilered_by_default(status):
    import re
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = replace(data.matches[0], status=status, team1_score=0, team2_score=0)
    text = load_formatters(data)["format_match"](match, "Europe/Berlin")
    assert ("||**C9** **0–0** **KC**||" if status == MatchStatus.COMPLETED else "||C9 0–0 KC||") in text
    assert "0–0" not in re.sub(r"\|\|.*?\|\|", "", text)


def test_votes_command_never_leaks_open_or_unrevealed_predictions(tmp_path, monkeypatch):
    import discord
    from services import user_settings
    from services.polls import cast_vote, create_poll, get_upcoming_match_votes, initialize_polls, reconcile_polls
    from services.delivery_records import DeliveryRecord, initialize_delivery_records, record_delivery
    from services.prediction_display import hidden_votes_payload, upcoming_votes_embed
    from services.tournament_queries import get_matches_for_votes
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "privacy.db")
    user_settings.initialize_database()
    initialize_polls()
    initialize_delivery_records()
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    match = data.matches[0]
    data = replace(data, matches=[match])
    now = [match.start_time - timedelta(hours=1)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]
    create_poll(123, match)
    cast_vote(123, match.id, 7, match.team1_id, now[0], "SECRET VOTER")
    callback = load_command("currentvotes", data, asyncio=asyncio, datetime=Clock, UTC=UTC, discord=discord,
        get_recent_completed_match_ids=lambda *args: set(), get_matches_for_votes=get_matches_for_votes,
        get_upcoming_match_votes=get_upcoming_match_votes, upcoming_votes_embed=upcoming_votes_embed,
        hidden_votes_payload=hidden_votes_payload)
    async def scenario():
        for closed in (False, True):
            if closed:
                now[0] = match.start_time - timedelta(minutes=5)
                reconcile_polls(123, data, now[0])
            interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
            await callback(interaction)
            text = rendered_message(interaction.followup.send.call_args).description
            assert "Voting closed" in text or "Voting for this match closes in" in text
            assert "SECRET VOTER" not in text and "%" not in text and "(1 vote)" not in text
        record_delivery(DeliveryRecord(123, match.id, 456, 789, now[0]))
        interaction = SimpleNamespace(guild_id=123, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await callback(interaction)
        embed = rendered_message(interaction.followup.send.call_args)
        assert "100%" in embed.fields[0].value
        assert "@SECRET VOTER" in embed.fields[0].value
        assert interaction.followup.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    asyncio.run(scenario())


def test_leaderboard_adds_fire_only_to_streaks_of_three_or_more():
    import discord
    from services.prediction_stats import PredictionStats
    data = load_tournament_data(PROJECT_ROOT / "data" / "worlds_2026.json")
    rows = [PredictionStats(7, 4, 5, 8, 3, 3), PredictionStats(8, 3, 5, 8, 2, 2)]
    members = {uid: SimpleNamespace(display_name=f"Friend {uid}") for uid in (7, 8)}
    interaction = SimpleNamespace(guild_id=123, guild=SimpleNamespace(get_member=members.get),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    context = AsyncMock(return_value=(None, [(row, members[row.user_id].display_name) for row in rows], None))
    callback = load_command("leaderboard", data, discord=discord, leaderboard_context=context)
    asyncio.run(callback(interaction))
    text = rendered_message(interaction.followup.send.call_args).description
    assert "🔥 Current streak: 3" in text
    assert "Current streak: 2" in text and "🔥 Current streak: 2" not in text
    assert "points" not in text
