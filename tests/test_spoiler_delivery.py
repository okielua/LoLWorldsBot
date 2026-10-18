import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from models import MatchStatus
from services import spoilers, user_settings
from services.polls import initialize_polls
from services.spoiler_delivery import SpoilerDelivery, render_payload
from services.spoilers import SpoilerLedger, SpoilerPolicy, aggregate_token, matchup, score_token, team_token
from services.team_emojis import configure_team_emojis
from test_spoilers import scenario


@pytest.fixture
def protected(monkeypatch):
    end, match, data = scenario()
    data.tournament = SimpleNamespace(id="worlds")
    future = type(match)("future", "worlds", "swiss", "T1", "MVK", end + timedelta(hours=10), 5)
    data.matches.append(future)
    data.matches_by_id[future.id] = future
    policy = SpoilerPolicy(data, ended_at={match.id: end}, dependencies={(future.id, 2): [match.id]}, clock=lambda: end)
    monkeypatch.setattr(spoilers, "_current", policy)
    configure_team_emojis([discord.PartialEmoji(name="t1", id=111), discord.PartialEmoji(name="MVK", id=222),
                           discord.PartialEmoji(name="beeQuestion", id=333)], data.teams)
    yield policy, match, future, end
    configure_team_emojis(())


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "test.db")
    user_settings.initialize_database()
    initialize_polls()
    return user_settings.get_connection


def test_wire_payload_protects_only_dependent_team_and_button_uses_bee_question(protected):
    policy, match, future, end = protected
    draft = {"content": matchup(future) + "\n" + score_token(match), "components": [
        {"type": 1, "components": [
            {"type": 2, "style": 1, "label": team_token("T1", future.id, 1), "emoji": {"id": "111", "name": "t1"}, "custom_id": "first"},
            {"type": 2, "style": 1, "label": team_token("MVK", future.id, 2), "emoji": {"id": "222", "name": "MVK"}, "custom_id": "second"}]}]}
    original = deepcopy(draft)
    result = render_payload(draft)
    assert result["content"] == "<:t1:111> `\xa0\xa0T1\xa0\xa0` vs ||<:MVK:222> `\xa0MVK\xa0\xa0`||\n||3–1||"
    first, second = result["components"][0]["components"]
    assert first["label"] == "\xa0\xa0T1\xa0\xa0"
    assert first["emoji"]["id"] == "111"
    assert second["label"] == "[see spoiler]"
    assert second["emoji"]["id"] == "333"
    assert second["custom_id"] == "second"
    assert draft == original
    policy.clock = lambda: end + timedelta(hours=8)
    released = render_payload(draft)
    assert "||" not in released["content"]
    assert released["components"][0]["components"][1]["emoji"]["id"] == "222"


def test_select_operational_values_and_emoji_names_are_untouched(protected):
    draft = {"components": [{"type": 3, "custom_id": "team:MVK", "options": [
        {"label": "MVK", "value": "MVK", "emoji": {"name": "MVK", "id": "222"}}]}]}
    option = render_payload(draft)["components"][0]["options"][0]
    assert option["value"] == "MVK"
    assert option["emoji"] == {"name": "MVK", "id": "222"}
    assert "`" not in option["label"]


def test_live_aggregates_are_withheld_and_completed_aggregates_fully_concealed(protected):
    policy, match, _, end = protected
    content = aggregate_token("1. Alice: 1 correct\n2. Bob: 0 correct")
    hidden = render_payload({"content": content})["content"]
    assert hidden == "||1. Alice: 1 correct||\n||2. Bob: 0 correct||"
    match.status = MatchStatus.LIVE
    live = render_payload({"content": content})["content"]
    assert "Alice" not in live and "1 correct" not in live
    match.status = MatchStatus.COMPLETED
    policy.clock = lambda: end + timedelta(hours=14)
    assert "||" not in render_payload({"content": content})["content"]


def test_release_edits_existing_message_silently_after_restart(protected, database):
    policy, match, future, end = protected
    http = AsyncMock(return_value={})
    client = SimpleNamespace(http=SimpleNamespace(request=http))
    initial = SpoilerDelivery(client, database)
    draft = {"content": matchup(future) + "\n" + score_token(match), "allowed_mentions": {"parse": ["everyone"]}}
    initial.remember({"id": "42", "channel_id": "12"}, draft, render_payload(draft))
    restarted = SpoilerDelivery(client, database)
    asyncio.run(restarted.release_once())
    http.assert_not_awaited()
    policy.clock = lambda: end + timedelta(hours=8)
    asyncio.run(restarted.release_once())
    http.assert_awaited_once()
    payload = http.call_args.kwargs["json"]
    assert "||" not in payload["content"]
    assert "⟦" not in json.dumps(payload)
    assert payload["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert http.call_args.args[0].method == "PATCH"
    assert "42" in http.call_args.args[0].url
    asyncio.run(restarted.release_once())
    http.assert_awaited_once()


def test_ephemeral_replies_are_not_saved(protected, database):
    _, match, _, _ = protected
    delivery = SpoilerDelivery(SimpleNamespace(http=SimpleNamespace(request=AsyncMock())), database)
    draft = {"content": score_token(match), "flags": 64}
    delivery.remember({"id": "42", "channel_id": "12"}, draft, render_payload(draft))
    with closing(database()) as db:
        assert db.execute("SELECT COUNT(*) FROM spoiler_messages").fetchone()[0] == 0


def test_completion_and_per_slot_provenance_survive_restart(protected, database):
    _, match, future, end = protected
    data = spoilers.current_policy().data
    # KC's prior fixture is complete; T1 is independently announced.
    ledger = SpoilerLedger(database)
    first = ledger.observe(data, end)
    assert first.ended_at[match.id] == end
    assert not first.participant_protected(future, 1, end)
    assert first.participant_protected(future, 2, end)
    second = SpoilerLedger(database).observe(data, end + timedelta(hours=1))
    assert second.ended_at[match.id] == end
    assert second.dependencies == first.dependencies


def test_already_known_slot_is_not_reclassified_when_a_series_finishes(protected, database):
    _, match, future, end = protected
    data = spoilers.current_policy().data
    match.status = MatchStatus.LIVE
    ledger = SpoilerLedger(database)
    ledger.observe(data, end - timedelta(minutes=1))
    match.status = MatchStatus.COMPLETED
    policy = ledger.observe(data, end)
    assert not policy.participant_protected(future, 2, end)


def test_http_and_interaction_webhook_transports_use_same_guard(protected, database, monkeypatch):
    from discord.http import Route
    from discord.webhook.async_ import AsyncWebhookAdapter

    _, match, future, _ = protected
    response = {"id": "42", "channel_id": "12"}
    rest = AsyncMock(return_value=response)
    webhook = AsyncMock(return_value={"resource": {"message": response}})
    monkeypatch.setattr(AsyncWebhookAdapter, "request", webhook)
    client = SimpleNamespace(http=SimpleNamespace(request=rest))
    delivery = SpoilerDelivery(client, database)
    delivery.install()
    raw = {"content": matchup(future) + "\n" + score_token(match)}

    async def send():
        await client.http.request(Route("POST", "/channels/{channel_id}/messages", channel_id=12), json=raw)
        adapter = AsyncWebhookAdapter()
        await adapter.request(Route("POST", "/interactions/{webhook_id}/{webhook_token}/callback", webhook_id=1, webhook_token="not-stored"),
                              session=None, payload={"type": 4, "data": raw})
    try:
        asyncio.run(send())
        assert "||3–1||" in rest.call_args.kwargs["json"]["content"]
        assert rest.call_args.kwargs["json"]["content"] == webhook.call_args.kwargs["payload"]["data"]["content"]
        with closing(database()) as db:
            stored = db.execute("SELECT draft FROM spoiler_messages").fetchone()[0]
            assert "not-stored" not in stored
    finally:
        delivery.uninstall()


def test_native_field_name_moves_into_spoiler_capable_body(protected):
    _, _, future, _ = protected
    draft = {"embeds": [{"title": "Voting Results", "fields": [
        {"name": team_token("MVK", future.id, 2), "value": "50%\n(2 votes)", "inline": True}]}]}
    field = render_payload(draft)["embeds"][0]["fields"][0]
    assert field["name"] == "\xa0"
    assert field["value"].startswith("||<:MVK:222> `\xa0MVK\xa0\xa0`||")
    assert field["inline"] is True


def test_partial_edit_keeps_original_score_draft_for_later_release(protected, database):
    policy, match, _, end = protected
    http = AsyncMock(return_value={})
    delivery = SpoilerDelivery(SimpleNamespace(http=SimpleNamespace(request=http)), database)
    response = {"id": "42", "channel_id": "12"}
    original = {"content": score_token(match)}
    delivery.remember(response, original, render_payload(original))
    change = {"components": []}
    delivery.remember(response, change, render_payload(change))
    # A partial component edit must not discard the original body.
    policy.clock = lambda: end + timedelta(hours=14)
    asyncio.run(delivery.release_once())
    assert http.call_args.kwargs["json"]["content"] == "3–1"


def test_recent_legacy_schedule_keeps_known_team_and_conceals_new_slot(protected):
    from services.spoiler_legacy import legacy_payload

    _, match, _, _ = protected
    raw = {"components": [{"type": 17, "components": [{"type": 10,
        "content": "## T1 vs MVK\n18 October 2026 · Bo5"}]}]}
    draft = legacy_payload(raw)
    text = render_payload(draft)["components"][0]["components"][0]["content"]
    assert "<:t1:111> `\xa0\xa0T1\xa0\xa0`" in text
    assert "||<:MVK:222> `\xa0MVK\xa0\xa0`||" in text
    assert "18 October 2026 · Bo5" in text


def test_legacy_live_score_is_removed_even_without_a_fixture_header(protected):
    from services.spoiler_legacy import legacy_payload

    raw = {"embeds": [{"description": "🔴 LIVE · ||1–2||"}]}
    text = render_payload(legacy_payload(raw))["embeds"][0]["description"]
    assert "1–2" not in text


def test_hidden_rankings_use_neutral_order_then_restore_rank_order(protected):
    policy, _, _, end = protected
    draft = {"content": aggregate_token("### `1.` Zoe: 2 correct\nDetails\n\n### `2.` Amy: 1 correct\nDetails\n")}
    hidden = render_payload(draft)["content"]
    assert hidden.index("Amy") < hidden.index("Zoe")
    assert all(line.startswith("||") for line in hidden.splitlines() if line.strip())
    policy.clock = lambda: end + timedelta(hours=14)
    released = render_payload(draft)["content"]
    assert released.index("Zoe") < released.index("Amy")
    assert "||" not in released


def test_empty_source_does_not_publish_unchecked_external_stats(protected):
    policy, _, _, _ = protected
    policy.data.matches = []
    policy.data.matches_by_id = {}
    payload = render_payload({"content": aggregate_token("Games: 3\nWin rate: 100%")})
    assert "100%" not in payload["content"]
    assert "unavailable" in payload["content"]


def test_reopened_series_gets_a_new_completion_observation(protected, database):
    _, match, _, end = protected
    data = spoilers.current_policy().data
    ledger = SpoilerLedger(database)
    ledger.observe(data, end)
    match.status = MatchStatus.LIVE
    ledger.observe(data, end + timedelta(minutes=1))
    match.status = MatchStatus.COMPLETED
    new_end = end + timedelta(hours=1)
    policy = SpoilerLedger(database).observe(data, new_end)
    assert policy.ended_at[match.id] == new_end


def test_personal_stats_protect_values_but_keep_labels_and_explanations_readable(protected):
    from services.leaderboard_display import personal_stats_embed
    from services.prediction_stats import CompetitionSnapshot, Prediction, PredictionStats

    policy, match, _, end = protected
    pick = Prediction(match.id, match.team1_id, match.team2_id, match.start_time, match.winner_id, match.winner_id)
    snapshot = CompetitionSnapshot([PredictionStats(7, 1, 1, 1, 1, 1)], None, {7: [pick]}, {}, 1,
                                   result_match_ids=(match.id,))
    draft = {"embeds": [personal_stats_embed(snapshot, 7, "lua").to_dict()]}
    text = render_payload(draft)["embeds"][0]["description"]
    assert "**My rank: ||1 of 1||**" in text
    assert "**My correct predictions: ||1||**" in text
    assert "__Check Your PayPal__" in text
    assert "__Can’t Read These Frauds__" in text
    assert "Requires at least 3 scored predictions" in text
    assert "completed series from Swiss Stage onward" in text
    assert not text.startswith("||")
    policy.clock = lambda: end + timedelta(hours=14)
    assert "||" not in render_payload(draft)["embeds"][0]["description"]


def test_unrelated_protected_result_does_not_hide_personal_stats(protected):
    from dataclasses import replace
    from services.leaderboard_display import personal_stats_embed
    from services.prediction_stats import CompetitionSnapshot, Prediction, PredictionStats

    policy, protected_match, _, end = protected
    released = replace(protected_match, id="released", start_time=end - timedelta(hours=19))
    policy.data.matches.append(released)
    policy.data.matches_by_id[released.id] = released
    policy.ended_at[released.id] = end - timedelta(hours=15)
    pick = Prediction(released.id, released.team1_id, released.team2_id, released.start_time, released.winner_id, released.winner_id)
    snapshot = CompetitionSnapshot([PredictionStats(7, 1, 1, 1, 1, 1)], None, {7: [pick]}, {}, 1,
                                   result_match_ids=(released.id,))
    assert policy.protected(protected_match)
    assert not policy.protected(released)
    text = render_payload({"embeds": [personal_stats_embed(snapshot, 7, "lua").to_dict()]})["embeds"][0]["description"]
    assert "||" not in text
    assert "**My correct predictions: 1**" in text
