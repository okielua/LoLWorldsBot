import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from models import MatchStatus
from services import spoilers, user_settings
from services.polls import initialize_polls
from services.spoiler_delivery import SpoilerDelivery, render_payload
from services.spoilers import SpoilerLedger, SpoilerPolicy, aggregate_token, date_heading_token, matchup, score_token, team_token
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


def test_disabled_masking_reveals_results_stats_and_both_participants(protected, monkeypatch):
    policy, match, future, _ = protected
    monkeypatch.setattr(policy, 'enabled', False)
    content = (matchup(future) + '\n' + score_token(match)
               + f'\n⟦result:{match.id}⟧✅ Correct⟦/result⟧'
               + f'\n⟦aggregate:{match.id},missing⟧Leaderboard: ||5 points||⟦/aggregate⟧')
    draft = {'content': content, 'components': [
        {'type': 1, 'components': [{'type': 2, 'style': 1,
         'custom_id': 'worldsvote:123:future:2', 'label': team_token(future.team2_id, future.id, 2),
         'emoji': {'id': '222', 'name': 'MVK'}}]}],
        'embeds': [{'description': '||A result||', 'fields': [{'name': '||Votes||', 'value': '||@Lua||'}]}]}
    rendered = render_payload(draft)
    serialized = json.dumps(rendered, ensure_ascii=False)
    assert '||' not in serialized and '⟦' not in serialized
    assert '✅ Correct' in serialized and '5 points' in serialized
    assert '[see spoiler]' not in serialized and 'Stats hidden' not in serialized
    button = rendered['components'][0]['components'][0]
    assert button['custom_id'] == 'worldsvote:123:future:2'
    assert 'MVK' in button['label'] and button['emoji']['id'] == '222'


def test_disabled_masking_survives_source_refresh_and_ledger_restart(database, monkeypatch):
    end, match, data = scenario()
    data.tournament = SimpleNamespace(id='worlds')
    monkeypatch.setattr(spoilers, '_current', None)
    for ledger in (SpoilerLedger(database, enabled=False), SpoilerLedger(database, enabled=False)):
        policy = ledger.observe(data)
        assert not policy.enabled and not policy.protected(match)
        assert policy.score(match) == '3–1'
        assert '||' not in render_payload({'content': '||Legacy tags|| ' + score_token(match)})['content']


@pytest.mark.parametrize("match_id", ["future", "prediction-test:68c569d2-e05e-4dab-9b1e-ea9a19d4b6e7:0"])
def test_wire_payload_protects_only_dependent_team_and_button_uses_bee_question(protected, match_id):
    policy, match, future, end = protected
    original_id = future.id
    future.id = match_id
    policy.data.matches_by_id.pop(original_id)
    policy.data.matches_by_id[future.id] = future
    policy.dependencies[(future.id, 2)] = policy.dependencies.pop((original_id, 2))
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
    policy.clock = lambda: end + timedelta(hours=12)
    released = render_payload(draft)
    assert "||" not in released["content"]
    assert released["components"][0]["components"][1]["emoji"]["id"] == "222"


def test_simulation_ids_outside_live_schedule_never_leak_internal_tokens(protected):
    match_id = "prediction-test:68c569d2-e05e-4dab-9b1e-ea9a19d4b6e7:0"
    draft = {"content": team_token("T1", match_id, 1) + " vs " + team_token("MVK", match_id, 2),
             "components": [{"type": 2, "style": 1, "custom_id": "test-vote",
                             "label": team_token("MVK", match_id, 2)}]}
    result = render_payload(draft)
    assert "⟦" not in str(result)
    assert "prediction-test:" not in str(result)
    assert "T1" in result["content"] and "MVK" in result["content"]
    assert result["components"][0]["label"] == "[see spoiler]"
    forced_neutral = render_payload({"content": team_token("MVK", match_id, 2, neutral=True)})
    assert "⟦" not in forced_neutral["content"]
    assert "beeQuestion" in forced_neutral["content"]


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
    policy.clock = lambda: end + timedelta(hours=12)
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
    policy.clock = lambda: end + timedelta(hours=12, microseconds=-1)
    asyncio.run(restarted.release_once())
    http.assert_not_awaited()
    policy.clock = lambda: end + timedelta(hours=12)
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


def test_public_date_header_updates_across_midnight_without_reposting_or_pinging(protected, database):
    policy, _, _, end = protected
    http = AsyncMock(return_value={})
    delivery = SpoilerDelivery(SimpleNamespace(http=SimpleNamespace(request=http)), database)
    draft = {"content": "## __" + date_heading_token(date(2026, 10, 19), "Europe/Berlin") + "__"}
    assert render_payload(draft)["content"] == "## __Tomorrow (Monday, 19.10.2026)__"
    delivery.remember({"id": "42", "channel_id": "12"}, draft, render_payload(draft))
    policy.clock = lambda: end + timedelta(days=1)
    asyncio.run(delivery.release_once())
    assert http.call_args.kwargs["json"]["content"] == "## __Today (Monday, 19.10.2026)__"
    policy.clock = lambda: end + timedelta(days=2)
    asyncio.run(delivery.release_once())
    assert http.call_args.kwargs["json"]["content"] == "## __Monday, 19 October 2026__"
    assert http.call_args.kwargs["json"]["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert all(call.args[0].method == "PATCH" for call in http.call_args_list)


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
    policy.clock = lambda: end + timedelta(hours=12)
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


@pytest.mark.parametrize("text", [
    "## __Pick'ems Leaderboard__\n\n[Go to our official Pick'ems leaderboard](<https://example.com/pickems>)",
    "# __Worlds Bot: List of all Commands__\n\n`/leaderboard`\nSee the prediction rankings.",
    "## Server Setup\n\n### Leaderboards\nSet the official Pick'ems link.",
    "-# • Use `/votehistory` to review predictions.\n-# • Use `/leaderboard` for rankings.",
    "No results yet. The standings update as matches finish.",
    "## __Communism Worlds Leaderboard__\n\nNo results yet. The standings update as matches finish.\n\n-# • Ranked by correct predictions, then accuracy.",
])
def test_navigation_and_instruction_messages_never_get_result_spoilers(protected, text):
    from services.spoiler_legacy import legacy_payload
    raw = {"components": [{"type": 10, "content": text}]}
    assert "⟦aggregate:" not in str(legacy_payload(raw))
    assert "||" not in str(render_payload(legacy_payload(raw)))
    wrongly_tracked = {"components": [{"type": 10, "content": aggregate_token(text)}]}
    assert "||" not in str(render_payload(wrongly_tracked))
    assert "⟦" not in str(render_payload(wrongly_tracked))


def test_link_classifier_still_protects_actual_prediction_tables(protected):
    from services.spoiler_legacy import legacy_payload
    raw = {"content": "## Communism Worlds Leaderboard\n\n### `1.` Lua: 5 correct\n[Help](https://example.com)"}
    assert "⟦aggregate:" in str(legacy_payload(raw))
    assert "||" in render_payload(legacy_payload(raw))["content"]
    # A heading/link next to actual points must not qualify as navigation-only.
    real_data = {"content": aggregate_token("## Pick'ems Leaderboard\n[Open](https://example.com)\nLua: 12 points")}
    assert "||" in render_payload(real_data)["content"]


def test_empty_leaderboard_is_visible_during_result_protection(protected):
    from services.leaderboard_display import leaderboard_embeds
    embed, = leaderboard_embeds([])
    assert "⟦aggregate:" not in embed.description
    result = render_payload({"embeds": [embed.to_dict()]})
    assert "No results yet." in result["embeds"][0]["description"]
    assert "||" not in result["embeds"][0]["description"]


def test_spoiler_refresh_cannot_overwrite_new_public_voter_list(protected, database):
    from discord.http import Route
    policy, _, future, end = protected

    async def scenario():
        old = {"content": team_token("MVK", future.id, 2) + "\n@Lua"}
        new = {"content": old["content"] + "\n@Miwa"}
        worker_started, finish_worker = asyncio.Event(), asyncio.Event()
        writes = []

        async def request(route, **kwargs):
            if not worker_started.is_set():
                worker_started.set()
                await finish_worker.wait()
            writes.append(kwargs["json"]["content"])
            return {"id": "42", "channel_id": "12"}

        client = SimpleNamespace(http=SimpleNamespace(request=request))
        delivery = SpoilerDelivery(client, database)
        delivery.remember({"id": "42", "channel_id": "12"}, old, render_payload(old))
        policy.clock = lambda: end + timedelta(hours=12)
        delivery.install()
        try:
            release = asyncio.create_task(delivery.release_once())
            await asyncio.wait_for(worker_started.wait(), timeout=5)
            edit = asyncio.create_task(client.http.request(
                Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=12, message_id=42), json=new))
            await asyncio.sleep(0)
            finish_worker.set()
            await asyncio.gather(release, edit)
            assert "@Miwa" in writes[-1]
            with closing(database()) as db:
                draft, rendered = db.execute("SELECT draft, rendered FROM spoiler_messages").fetchone()
            assert "@Miwa" in draft and "@Miwa" in rendered
        finally:
            finish_worker.set()
            delivery.uninstall()

    asyncio.run(scenario())


def test_wrongly_hidden_link_post_is_repaired_by_silent_edit(protected, database):
    http = AsyncMock(return_value={})
    delivery = SpoilerDelivery(SimpleNamespace(http=SimpleNamespace(request=http)), database)
    text = "## __Pick'ems Leaderboard__\n\n[Go to our official Pick'ems leaderboard](<https://example.com>)"
    draft = {"content": aggregate_token(text)}
    delivery.remember({"id": "42", "channel_id": "12"}, draft, {"content": "||wrongly hidden||"})
    asyncio.run(delivery.release_once())
    http.assert_awaited_once()
    assert http.call_args.kwargs["json"]["content"] == text
    assert http.call_args.kwargs["json"]["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert http.call_args.args[0].method == "PATCH"


def test_modern_result_free_messages_are_excluded_from_legacy_adoption(protected, database):
    http = AsyncMock(return_value={})
    message = SimpleNamespace(id=42, author=SimpleNamespace(id=99))
    async def history(**kwargs):
        yield message
    channel = SimpleNamespace(id=12, history=history)
    client = SimpleNamespace(http=SimpleNamespace(request=http), user=SimpleNamespace(id=99),
                             guilds=[SimpleNamespace(text_channels=[channel], threads=[])])
    delivery = SpoilerDelivery(client, database)
    delivery.remember({"id": "42", "channel_id": "12"}, {"content": "Plain modern message"}, {"content": "Plain modern message"})
    asyncio.run(delivery.adopt_recent_messages())
    http.assert_not_awaited()
    with closing(database()) as db:
        assert db.execute("SELECT COUNT(*) FROM spoiler_managed_messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM spoiler_messages").fetchone()[0] == 0


def test_late_bootstrap_keeps_old_known_kc_visible_and_recent_opponent_protected(protected, database):
    from dataclasses import replace
    from models import Team
    from services.team_emojis import team_emoji
    _, prior, future, now = protected
    prior = replace(prior, start_time=now - timedelta(days=2), team1_id="KC", team2_id="MVK")
    later = replace(prior, id="later", start_time=now - timedelta(days=1), team1_id="MVK", team2_id="FUR")
    future = replace(future, team1_id="KC", team2_id="MVK", start_time=now + timedelta(hours=3))
    data = spoilers.current_policy().data
    data.matches = [prior, later, future]
    data.matches_by_id = {match.id: match for match in data.matches}
    data.teams.append(Team("FUR", "FUR", "FUR"))
    data.teams_by_id["FUR"] = data.teams[-1]
    configure_team_emojis([discord.PartialEmoji(name="KC", id=444), discord.PartialEmoji(name="MVK", id=222),
                           discord.PartialEmoji(name="beeQuestion", id=333)], data.teams)
    # Reproduce an old database that first noticed both finishes on startup.
    ledger = SpoilerLedger(database)
    with closing(database()) as db, db:
        for match in (prior, later):
            db.execute("INSERT INTO spoiler_ends VALUES (?, ?, ?)", (data.tournament.id, match.id, now.isoformat()))
    policy = ledger.observe(data, now)
    policy.clock = lambda: now
    assert policy.release_at(prior) == later.start_time + timedelta(hours=12)
    assert not policy.participant_protected(future, 1)
    assert policy.participant_protected(future, 2)
    raw = {"components": [{"type": 2, "label": team_token("KC", future.id, 1),
                           "emoji": {"name": "KC", "id": "444"}}]}
    kc = render_payload(raw)["components"][0]
    assert kc["label"].strip("\xa0") == "KC"
    assert kc["emoji"] == {"name": "KC", "id": "444"}
    assert "||" not in policy.team("KC", match=future, slot=1, emoji=team_emoji("KC"))
    restarted = ledger.observe(data, now + timedelta(minutes=1))
    assert restarted.ended_at[prior.id] == later.start_time


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
    policy.clock = lambda: end + timedelta(hours=12)
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
    assert "### · Current rank: ||1 of 1||" in text
    assert "### · Correct predictions: ||1/1|| (||100%||)" in text
    assert "__*Check Your PayPal*__" in text
    assert "__*Can’t Read These Frauds*__" in text
    assert "Requires at least 3 scored predictions" in text
    assert "completed series across Worlds, including Play-Ins" in text
    assert not text.startswith("||")
    policy.clock = lambda: end + timedelta(hours=12)
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
    assert "### · Correct predictions: 1/1 (100%)" in text
