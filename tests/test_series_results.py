import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from models import MatchStatus
from services import polls, spoilers
from services.series_results import SeriesResults, latest_result, result_pages, show_latest_results
from services.server_settings import set_worlds_role
from services.spoiler_delivery import render_payload
from services.voting import VotingManager
from test_voting import context
from presentation_assertions import rendered_message


def complete(store, now, match):
    finished = replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id,
                       team1_score=3, team2_score=1)
    store.data = replace(store.data, matches=[finished if item.id == match.id else item for item in store.data.matches])
    now[0] = max(now[0], match.start_time + timedelta(hours=3))
    store.fetched_at = now[0]
    return finished


def test_completion_announces_once_and_latest_survives_restart(context):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        await manager.vote(first.id, 8, first.team2_id, "Miwa")
        complete(store, now, first)
        await manager.tick()
        card = rendered_message(channel.send.call_args)
        notice = card.title + "\n" + card.description
        assert "Series Voting Results" in notice
        assert "@Lua" in notice and "@Miwa" in notice
        assert notice.count("· 1 vote (50%)") == 2
        assert "Voted for" not in notice
        assert "Correct predictions" not in notice and "Incorrect predictions" not in notice
        assert "2 votes cast" in notice and "50%" in notice
        latest = latest_result(123, store.data.tournament.id)
        assert latest and "@Miwa" in latest[2][0]
        sent = channel.send.await_count
        await manager.tick()
        restarted = VotingManager(manager.client, store, 123, clock=lambda: now[0])
        await restarted.tick()
        assert channel.send.await_count == sent
        assert latest_result(999, store.data.tournament.id) is None
    asyncio.run(scenario())


def test_initial_install_does_not_announce_historical_results(context):
    manager, store, channel, now, _ = context
    complete(store, now, store.data.matches[0])
    asyncio.run(manager.tick())
    assert all("Series Voting Results" not in rendered_message(call).description for call in channel.send.call_args_list)
    assert latest_result(123, store.data.tournament.id) is None


def test_result_without_retry_sends_with_an_advancing_clock(context):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, 'Lua')
        complete(store, now, first)
        polls.reconcile_polls(manager.guild_id, store.data, now[0])
        def advancing_clock():
            now[0] += timedelta(microseconds=1)
            return now[0]
        manager.clock = advancing_clock
        channel.send.reset_mock()
        await manager.series_results.tick()
        channel.send.assert_awaited_once()
        assert latest_result(123, store.data.tournament.id)
        await manager.series_results.tick()
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_role_and_both_team_subscribers_are_deduplicated(context, monkeypatch):
    manager, store, channel, now, _ = context
    set_worlds_role(123, 88)
    monkeypatch.setattr("services.series_results.get_all_user_reminder_preferences", lambda: [])
    monkeypatch.setattr("services.series_results.get_server_ping_user_ids", lambda *args: [7, 7, 8, 9])
    channel.guild.get_member = lambda uid: SimpleNamespace(roles=[SimpleNamespace(id=88)] if uid == 8 else [])
    async def scenario():
        await manager.tick()
        complete(store, now, store.data.matches[0])
        await manager.tick()
        call = channel.send.call_args
        text = rendered_message(call).description
        assert text.count("<@&88>") == text.count("<@7>") == text.count("<@9>") == 1
        assert "<@8>" not in text
        mentions = call.kwargs["allowed_mentions"].to_dict()
        assert mentions["roles"] == [88] and mentions["users"] == [7, 9]
        assert "everyone" not in mentions.get("parse", [])
    asyncio.run(scenario())


def test_test_completion_never_pings_and_latest_command_is_public(context):
    manager, store, channel, now, _ = context
    manager.test_mode = True
    set_worlds_role(123, 88)
    async def scenario():
        await manager.tick()
        complete(store, now, store.data.matches[0])
        await manager.tick()
        call = channel.send.call_args
        card = rendered_message(call)
        text = card.title + "\n" + card.description
        assert "TEST: Series Voting Results" in text and "<@" not in text
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        interaction = SimpleNamespace(guild_id=123,
            client=SimpleNamespace(voting_manager=manager),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await show_latest_results(interaction)
        call = interaction.followup.send.call_args
        assert call.kwargs.get("ephemeral") is True
        assert "TEST: Series Voting Results" in rendered_message(call).title
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    asyncio.run(scenario())


@pytest.mark.parametrize("test_mode", [False, True])
def test_series_results_are_visible_without_changing_other_spoiler_rules(context, monkeypatch, test_mode):
    manager, store, _, now, _ = context
    match = complete(store, now, store.data.matches[0])
    policy = spoilers.SpoilerPolicy(store.data, ended_at={match.id: now[0]}, clock=lambda: now[0])
    monkeypatch.setattr(spoilers, "_current", policy)
    votes = [(i, match.team1_id if i % 2 else match.team2_id, f"Voter {i}") for i in range(300)]
    pages = result_pages(match, votes, test_mode=test_mode)
    assert len(pages) > 1 and all(len(page) < 3600 for page in pages)
    for page in pages:
        visible = render_payload({"content": page})["content"]
        assert "@Voter" in visible and "⟦" not in visible and "||" not in visible
        assert "Final score:** 3–1" in visible
        voter_lines = [line for line in visible.splitlines() if "@Voter" in line]
        assert all(line.startswith(("✅ @Voter", "❌ @Voter")) for line in voter_lines)
    ordinary = render_payload({"content": spoilers.score_token(match) + "\n" + spoilers.result_token(match, "✅")})
    assert "||3–1||" in ordinary["content"] and "||✅||" in ordinary["content"]
    now[0] += timedelta(hours=12)
    released = [render_payload({"content": page})["content"] for page in pages]
    assert all("||" not in page for page in released)
    voters = [line for page in released for line in page.splitlines() if "@Voter" in line]
    assert len(voters) == 300
    assert sum(line.startswith("✅ @Voter") for line in voters) == 150
    assert sum(line.startswith("❌ @Voter") for line in voters) == 150


def test_latest_stays_until_next_series_result_is_published(context):
    manager, store, _, now, _ = context
    async def scenario():
        await manager.tick()
        first, second = store.data.matches[:2]
        complete(store, now, first)
        await manager.tick()
        previous = latest_result(123, store.data.tournament.id)
        await manager.tick()
        assert latest_result(123, store.data.tournament.id) == previous
        complete(store, now, second)
        await manager.tick()
        assert latest_result(123, store.data.tournament.id)[2] != previous[2]
    asyncio.run(scenario())


def test_large_subscriber_groups_are_all_pinged_once(context, monkeypatch):
    manager, store, channel, now, _ = context
    monkeypatch.setattr("services.series_results.get_all_user_reminder_preferences", lambda: [])
    monkeypatch.setattr("services.series_results.get_server_ping_user_ids", lambda *args: list(range(1, 251)))
    async def scenario():
        await manager.tick()
        before = channel.send.await_count
        complete(store, now, store.data.matches[0])
        await manager.tick()
        calls = channel.send.call_args_list[before:]
        recipients = [uid for call in calls for uid in call.kwargs["allowed_mentions"].to_dict().get("users", [])]
        assert sorted(recipients) == list(range(1, 251))
        assert all(len(call.kwargs["allowed_mentions"].to_dict().get("users", [])) <= 90 for call in calls)
        count = channel.send.await_count
        await manager.tick()
        assert channel.send.await_count == count
    asyncio.run(scenario())


def test_manual_test_finish_publishes_results_without_real_votes_or_pings(context):
    from services.prediction_test import finish_prediction_test
    manager, store, channel, _, _ = context
    manager.test_mode = True
    manager.client.prediction_test_manager = manager
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        interaction = SimpleNamespace(client=manager.client)
        assert "Simulated series finishes" in await finish_prediction_test(interaction, 123)
        latest = latest_result(123, store.data.tournament.id)
        assert latest and "No votes were cast" in latest[2][0]
        notices = [call for call in channel.send.call_args_list if "Series Voting Results" in (rendered_message(call).title or "")]
        assert len(notices) == 2
        assert "@Lua" in rendered_message(notices[0]).description
        assert all(call.kwargs["allowed_mentions"].to_dict() == {"parse": []} for call in notices)
    asyncio.run(scenario())


def test_acknowledged_result_is_not_reposted_when_receipt_write_fails(context, monkeypatch):
    import sqlite3
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        complete(store, now, store.data.matches[0])
        original = manager.series_results._save
        def fail(*args):
            raise sqlite3.OperationalError("temporarily unavailable")
        monkeypatch.setattr(manager.series_results, "_save", fail)
        await manager.tick()
        assert manager.series_results._pending
        sent = channel.send.await_count
        monkeypatch.setattr(manager.series_results, "_save", original)
        await manager.tick()
        assert channel.send.await_count == sent
        assert latest_result(123, store.data.tournament.id)
        assert not manager.series_results._pending
    asyncio.run(scenario())


def test_live_or_unpublished_matches_never_announce_results(context):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        finished = complete(store, now, first)
        store.data = replace(store.data, matches=[replace(item, status=MatchStatus.LIVE) if item.id == first.id else item
                                                 for item in store.data.matches])
        sent = channel.send.await_count
        await manager.tick()
        assert channel.send.await_count == sent
        assert latest_result(123, store.data.tournament.id) is None
        store.data = replace(store.data, matches=[finished if item.id == first.id else item for item in store.data.matches])
        with polls.get_connection() as db:
            db.execute("UPDATE match_polls SET published_at=NULL WHERE guild_id=123 AND match_id=?", (first.id,))
        await manager.series_results.tick()
        assert channel.send.await_count == sent
    asyncio.run(scenario())


def test_existing_result_card_and_history_are_refreshed_without_pings(context):
    import json
    manager, store, channel, now, message = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        complete(store, now, first)
        await manager.tick()
        with polls.get_connection() as db:
            db.execute("UPDATE series_result_posts SET pages=? WHERE guild_id=123 AND match_id=?",
                       (json.dumps(["Old card with Correct predictions"]), first.id))
        sent = channel.send.await_count
        message.edit.reset_mock()
        await manager.series_results.tick()
        message.edit.assert_awaited_once()
        card = rendered_message(message.edit.call_args)
        assert "· 1 vote (100%)" in card.description and "@Lua" in card.description
        assert "Correct predictions" not in card.description
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert "· 1 vote (100%)" in latest_result(123, store.data.tournament.id)[2][0]
        assert channel.send.await_count == sent
    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["missing", "startup_skipped", "announced"])
def test_any_member_can_summon_results_without_an_automatic_post(context, receipt):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        await manager.vote(first.id, 8, first.team2_id, "Miwa")
        complete(store, now, first)
        if receipt == "announced":
            await manager.tick()
        elif receipt == "startup_skipped":
            with polls.get_connection() as db:
                db.execute("INSERT INTO series_result_posts VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
                           (123, store.data.tournament.id, first.id))
        count = channel.send.await_count
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=9),
            client=SimpleNamespace(voting_manager=manager),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await show_latest_results(interaction)
        call = interaction.followup.send.call_args
        card = rendered_message(call)
        assert "Series Voting Results" in card.title
        assert "Final score:** 3–1" in card.description
        assert "✅ @Lua" in card.description and "❌ @Miwa" in card.description
        assert "||" not in card.description
        assert ("Open the results announcement" in card.description) == (receipt == "announced")
        assert call.kwargs.get("ephemeral") is True
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert channel.send.await_count == count
        if receipt != "announced":
            assert latest_result(123, store.data.tournament.id) is None
    asyncio.run(scenario())


def test_summon_never_exposes_live_or_other_server_votes(context):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first = store.data.matches[0]
        await manager.vote(first.id, 7, first.team1_id, "Secret pick")
        finished = complete(store, now, first)
        interaction = SimpleNamespace(guild_id=999, client=SimpleNamespace(voting_manager=manager),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await show_latest_results(interaction)
        assert "Secret pick" not in rendered_message(interaction.followup.send.call_args).description
        interaction.guild_id = 123
        store.data = replace(store.data, matches=[replace(finished, status=MatchStatus.LIVE)])
        await show_latest_results(interaction)
        text = rendered_message(interaction.followup.send.call_args).description
        assert "Secret pick" not in text and "Final score:" not in text
    asyncio.run(scenario())


def test_summon_prefers_new_completed_vote_over_an_older_announcement(context):
    manager, store, channel, now, _ = context
    async def scenario():
        await manager.tick()
        first, second = store.data.matches[:2]
        await manager.vote(first.id, 7, first.team1_id, "Previous voter")
        await manager.vote(second.id, 8, second.team2_id, "Latest voter")
        complete(store, now, first)
        await manager.tick()
        assert latest_result(123, store.data.tournament.id)
        complete(store, now, second)
        interaction = SimpleNamespace(guild_id=123, client=SimpleNamespace(voting_manager=manager),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await show_latest_results(interaction)
        text = rendered_message(interaction.followup.send.call_args).description
        assert "Latest voter" in text and "Previous voter" not in text
        assert "Open the results announcement" not in text
    asyncio.run(scenario())
