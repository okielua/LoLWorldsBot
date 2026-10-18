"""Audit actual output builders and Discord surfaces against one team field."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import re
from types import SimpleNamespace

import discord
import pytest

from models import MatchStatus
from services import spoilers, voting
from services.polls import Poll
from services.presentation import message_payload, embed_card_payload
from services.prediction_display import upcoming_votes_embed, hidden_votes_payload, my_votes_embeds, change_vote_text
from services.prediction_stats import CompetitionSnapshot, PredictionStats, AdvancedPredictionStats, TeamPredictionStats
from services.leaderboard_display import personal_stats_embed
from services.spoiler_delivery import render_payload
from services.spoilers import SpoilerPolicy, padded_name
from services.standings_display import standings_text
from services.team_emojis import configure_team_emojis
from services.tournament_data import load_tournament_data
from test_bot_commands import load_command, load_formatters


@pytest.fixture
def context(monkeypatch):
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data/worlds_2026.json")
    source = data.matches[0]
    completed = replace(source, team1_id="KC", team2_id="MVK", status=MatchStatus.COMPLETED,
                        winner_id="KC", team1_score=3, team2_score=1)
    now = completed.start_time + timedelta(hours=5)
    future = replace(source, id="future", team1_id="T1", team2_id="MVK", start_time=now + timedelta(hours=12))
    data = replace(data, matches=[completed, future], standings=[])
    policy = SpoilerPolicy(data, ended_at={completed.id: now}, dependencies={(future.id, 2): [completed.id]}, clock=lambda: now)
    monkeypatch.setattr(spoilers, "_current", policy)
    monkeypatch.setattr(voting, "get_registered_timezones", lambda: [])
    configure_team_emojis([discord.PartialEmoji(name="KC", id=111), discord.PartialEmoji(name="MVK", id=222),
                           discord.PartialEmoji(name="t1", id=333), discord.PartialEmoji(name="beeQuestion", id=444)], data.teams)
    poll = Poll(123, future.id, data.tournament.id, future.team1_id, future.team2_id, future.start_time, False, None)
    past = Poll(123, completed.id, data.tournament.id, completed.team1_id, completed.team2_id,
                completed.start_time, True, completed.winner_id)
    manager = SimpleNamespace(store=SimpleNamespace(data=data), guild_id=123, test_mode=False, clock=lambda: now)
    yield SimpleNamespace(data=data, completed=completed, future=future, now=now, policy=policy, poll=poll, past=past, manager=manager)
    configure_team_emojis(())


def serialize(payload):
    raw = {key: value for key, value in payload.items() if key in {"content", "flags"}}
    if payload.get("view"):
        raw["components"] = payload["view"].to_components()
    if payload.get("embed"):
        raw["embeds"] = [payload["embed"].to_dict()]
    if payload.get("embeds"):
        raw["embeds"] = [embed.to_dict() for embed in payload["embeds"]]
    return render_payload(raw)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"custom_id", "url", "emoji", "allowed_mentions"}:
                yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def assert_centered(payload, policy):
    fields = [field for text in strings(payload) for field in re.findall(r"`([^`\n]+)`", text)
              if field.strip(" \xa0") in policy.data.teams_by_id]
    assert fields, "The output must exercise at least one team display"
    for field in fields:
        name = field.strip(" \xa0")
        assert field == padded_name(name, policy.width)
        assert len(field) == policy.width
        assert field.startswith("\xa0") and field.endswith("\xa0")
        assert "\u200b" not in field and "\u202f" not in field


@pytest.mark.parametrize("output", ["previous", "schedule", "next", "team-history", "currentvotes-open",
                                     "currentvotes-closed", "changevote", "myvotes", "votehistory",
                                     "daily-post", "schedule-votes", "personal-votes", "advanced-stats", "standings"])
def test_team_tags_share_width_across_actual_output_builders(context, output):
    c = context
    formatters = load_formatters(c.data)
    if output in {"previous", "schedule"}:
        match = c.completed if output == "previous" else c.future
        payload = message_payload(formatters["format_match"](match, "Europe/Berlin", now=c.now))
    elif output == "next":
        payload = message_payload(formatters["format_next_match"](c.future, "Europe/Berlin", c.now))
    elif output == "team-history":
        payload = message_payload("\n\n".join(formatters["format_team_matches"]([c.completed, c.future], ["Europe/Berlin"], c.now)))
    elif output == "currentvotes-open":
        payload = hidden_votes_payload(c.future, now=c.now)
    elif output == "currentvotes-closed":
        payload = {"embed": upcoming_votes_embed(c.completed, [(7, "KC", "Voter")])}
    elif output == "changevote":
        payload = message_payload(change_vote_text(c.future, now=c.now))
    elif output in {"myvotes", "votehistory"}:
        embed = my_votes_embeds([(c.past, "KC"), (c.poll, "MVK")], c.data, now=c.now)[0]
        payload = embed_card_payload(embed)
    elif output == "daily-post":
        day = c.future.start_time.astimezone(voting.MATCHDAY_ZONE).date().isoformat()
        payload = {"view": voting.DailyPredictionsView(c.manager, day, [c.future], {c.future.id: c.poll})}
    elif output == "schedule-votes":
        payload = voting.schedule_vote_payloads(c.manager, "Schedule", "", [(c.future, change_vote_text(c.future, now=c.now))], {c.future.id: c.poll})[0]
    elif output == "personal-votes":
        payload = voting.personal_vote_payloads(c.manager, [c.poll], 7, [(c.poll, "MVK")])[0]
    elif output == "advanced-stats":
        row = PredictionStats(7, 2, 3, 3, 1, 2)
        advanced = AdvancedPredictionStats(best_read=TeamPredictionStats("MVK", 2, 3, 2, 1))
        snapshot = CompetitionSnapshot([row], None, {}, {}, 3, {7: advanced})
        payload = embed_card_payload(personal_stats_embed(snapshot, 7, "Voter"))
    else:
        payload = message_payload(standings_text(c.data, [("KC", 1, 0), ("MVK", 0, 1)]))
    assert_centered(serialize(payload), c.policy)


@pytest.mark.parametrize("surface", ["content", "description", "field", "footer", "author", "component", "old-inline-code"])
def test_every_discord_text_surface_normalizes_team_fields(context, surface):
    text = "T1 vs MVK"
    if surface == "content":
        draft = {"content": text}
    elif surface == "component":
        draft = {"components": [{"type": 17, "components": [{"type": 10, "content": text}]}]}
    elif surface == "old-inline-code":
        draft = {"content": "<:t1:333> `T1` vs <:MVK:222> `MVK`"}
    elif surface == "field":
        draft = {"embeds": [{"fields": [{"name": text, "value": text}]}]}
    elif surface in {"footer", "author"}:
        draft = {"embeds": [{surface: {"text" if surface == "footer" else "name": text}}]}
    else:
        draft = {"embeds": [{"description": text}]}
    rendered = render_payload(draft)
    assert_centered(rendered, context.policy)
    if surface == "old-inline-code":
        assert rendered["content"].count("<:t1:333>") == 1
        assert rendered["content"].count("<:MVK:222>") == 1


def test_team_autocomplete_uses_same_plain_padded_width_without_changing_ids(context):
    callback = load_command("team_autocomplete", context.data, discord=discord,
                            app_commands=discord.app_commands, TBD_TEAM_ID="TBD")
    choices = asyncio.run(callback(None, ""))
    assert choices
    for choice in choices:
        assert choice.value in context.data.teams_by_id
        assert choice.name == padded_name(context.data.teams_by_id[choice.value].short_name, context.policy.width)


def test_team_selection_menu_and_selected_vote_buttons_keep_same_width(context):
    c = context
    option = discord.SelectOption(label=c.data.teams_by_id["MVK"].name, value="MVK", description="MVK",
                                  emoji=discord.PartialEmoji(name="MVK", id=222), default=True)
    draft = {"components": [{"type": 1, "components": [{"type": 3, "custom_id": "follow-teams",
                                                        "options": [option.to_dict()]}]}]}
    selected = render_payload(draft)["components"][0]["components"][0]["options"][0]
    assert selected["label"] == selected["description"] == padded_name("MVK", c.policy.width)
    assert selected["value"] == "MVK" and selected["default"] is True
    assert selected["emoji"]["id"] == 222
    view = voting.PersonalVoteView(c.manager, c.poll, 7, selected_team="T1")
    buttons = serialize({"view": view})["components"][0]["components"]
    assert buttons[0]["label"] == padded_name("T1", c.policy.width) + " ✓"
    assert buttons[1]["label"] == "Vote (see spoiler above)"
    assert buttons[1]["emoji"]["name"] == "beeQuestion"


def test_five_minute_reminder_uses_same_centered_fields(context, tmp_path, monkeypatch):
    from services import user_settings
    from services.delivery_records import initialize_delivery_records
    from test_server_reminders import make_delivery

    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "reminders.db")
    user_settings.initialize_database()
    initialize_delivery_records()
    delivery, store, channel, _, _ = make_delivery()
    store.data = context.data
    now = context.future.start_time - timedelta(minutes=5)
    store.fetched_at = now
    delivery.clock = lambda: now
    context.policy.clock = lambda: now
    assert asyncio.run(delivery.deliver(context.future.id))
    assert_centered(serialize(channel.send.call_args.kwargs), context.policy)


def test_non_team_code_and_urls_are_preserved(context):
    source = "Use `/team` and `/mysetup`. `1.` https://example.com/teams/MVK"
    assert render_payload({"content": source})["content"] == source
