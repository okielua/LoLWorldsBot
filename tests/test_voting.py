from presentation_assertions import rendered_message, rendered_payload
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from models import MatchStatus
from services import user_settings
from services.delivery_records import initialize_delivery_records
from services.polls import get_daily_post, get_leaderboard, get_poll_results, get_polls, initialize_polls
from services.server_reminders import ServerReminderDelivery
from services.server_settings import initialize_server_settings, set_reminder_channel, set_worlds_role
from services.tournament_data import load_tournament_data
from services.voting import VotingManager


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "voting.db")
    user_settings.initialize_database()
    initialize_server_settings()
    initialize_delivery_records()
    initialize_polls()
    for user_id in (7, 8, 9):
        user_settings.mark_setup_completed(123, user_id)
    set_reminder_channel(123, 456)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    current = [data.matches[0].start_time - timedelta(hours=12)]
    store = SimpleNamespace(data=data, fetched_at=current[0], last_error=None)
    channel = Mock(spec=discord.TextChannel)
    channel.id = 456
    channel.type = discord.ChannelType.text
    channel.guild = SimpleNamespace(id=123, me=object(), get_member=lambda user_id: SimpleNamespace(roles=[]))
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=789))
    message = SimpleNamespace(id=789, edit=AsyncMock())
    channel.fetch_message = AsyncMock(return_value=message)
    channel.get_partial_message.return_value = message
    client = SimpleNamespace(get_channel=Mock(return_value=channel), fetch_channel=AsyncMock(return_value=channel), add_view=Mock())
    manager = VotingManager(client, store, 123, clock=lambda: current[0])
    return manager, store, channel, current, message


def test_daily_post_opens_early_without_pings_and_survives_restart(context):
    manager, store, channel, current, _ = context
    async def scenario():
        current[0] -= timedelta(days=2)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        current[0] = store.data.matches[0].start_time - timedelta(hours=24)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_awaited_once()
        current[0] += timedelta(seconds=1)
        await manager.tick()
        await manager.tick()
        channel.send.assert_awaited_once()
        call = channel.send.call_args
        components = call.kwargs["view"].to_components()
        assert "Who wins?" in str(components)
        assert "**C9** vs **KC**" in str(components)
        assert "**FUR** vs **MVK**" in str(components)
        card, = call.kwargs["view"].children
        assert isinstance(card, discord.ui.Container)
        assert card.accent_colour.value == 0x5865F2
        assert "Play-ins · Opening Round · Bo5" in str(components)
        assert "# __Worlds 2026 Day 1/" in str(components)
        assert "· Play-ins__" in str(components)
        assert "Here are today's matchups. Voting for each matchup closes 5 minutes before match start." in str(components)
        assert "`/votehistory`" in str(components)
        assert "Communism's Worlds prediction standings" in str(components)
        assert card.children[-2].content == (
            "\u00a0\n\n"
            "-# • *Use `/vote` to change your prediction until 5 minutes before a match.*\n"
            "-# • *Use `/votehistory` for the latest completed series' voting results.*\n"
            "-# • *Use `/leaderboard` to see Communism's Worlds prediction standings.*"
        )
        assert "1 point" not in str(components)
        assert "stay private" not in str(components)
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert call.kwargs["view"].is_persistent()
        assert len([item for item in call.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]) == 4
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        channel.send.assert_awaited_once()
        assert manager.client.add_view.called
        await restarted.vote(store.data.matches[0].id, 7, "C9")
        assert get_leaderboard(123, store.data.tournament.id) == []
    asyncio.run(scenario())


def test_admin_delete_pauses_day_across_restarts_and_preserves_votes(context):
    from services import polls
    manager, store, channel, current, message = context
    message.delete = AsyncMock()

    async def scenario():
        await manager.tick()
        match = store.data.matches[0]
        await manager.vote(match.id, 7, match.team1_id, "Voter")
        day = await manager.delete_daily_post()
        message.delete.assert_awaited_once()
        assert get_daily_post(123, store.data.tournament.id, day) is None
        assert polls.get_latest_daily_post(123, store.data.tournament.id) is None
        assert polls.daily_post_suppressed(123, store.data.tournament.id, day)
        assert polls.get_user_votes(123, store.data.tournament.id, 7)[0][1] == match.team1_id
        initialize_polls()
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        channel.send.assert_awaited_once()
        await restarted.publish_daily_post()
        assert channel.send.await_count == 2
        assert not polls.daily_post_suppressed(123, store.data.tournament.id, day)
        await restarted.tick()
        assert channel.send.await_count == 2
    asyncio.run(scenario())


def test_manual_publish_pings_configured_role_once_and_reuses_existing_post(context):
    manager, store, channel, _, _ = context
    set_worlds_role(123, 444)
    manager._followers = AsyncMock(return_value=[7])

    async def scenario():
        first = await manager.publish_daily_post()
        second = await manager.publish_daily_post()
        assert first == second
        channel.send.assert_awaited_once()
        assert channel.send.call_args.kwargs["allowed_mentions"].to_dict() == {"roles": [444], "parse": []}
        assert "<@&444>" in str(channel.send.call_args.kwargs["view"].to_components())
        assert channel.get_partial_message.return_value.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        manager._followers.assert_not_awaited()
        await manager.tick()
        channel.send.assert_awaited_once()
        manager._followers.assert_not_awaited()
    asyncio.run(scenario())


def test_manual_publish_works_with_automatic_reminders_off(context):
    from services.server_settings import set_public_reminders_enabled
    manager, _, channel, _, _ = context
    set_public_reminders_enabled(123, False)
    asyncio.run(manager.publish_daily_post())
    channel.send.assert_awaited_once()


@pytest.mark.parametrize("blocked", ["too-early", "stale", "cutoff", "unknown-teams"])
def test_manual_publish_does_not_bypass_schedule_or_voting_safety(context, blocked):
    manager, store, channel, current, _ = context
    if blocked == "too-early":
        current[0] = store.data.matches[0].start_time - timedelta(hours=25)
        store.fetched_at = current[0]
    elif blocked == "stale":
        store.last_error = "offline"
    elif blocked == "cutoff":
        store.data = replace(store.data, matches=[store.data.matches[0]])
        current[0] = store.data.matches[0].start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
    else:
        store.data = replace(store.data, matches=[replace(match, team1_id="TBD") for match in store.data.matches])
    with pytest.raises(ValueError):
        asyncio.run(manager.publish_daily_post())
    channel.send.assert_not_awaited()


def test_failed_admin_delete_keeps_existing_post_and_restores_auto_updates(context):
    from services import polls
    manager, store, _, _, message = context
    message.delete = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied"))

    async def scenario():
        await manager.tick()
        before = polls.get_latest_daily_post(123, store.data.tournament.id)
        with pytest.raises(discord.Forbidden):
            await manager.delete_daily_post()
        assert polls.get_latest_daily_post(123, store.data.tournament.id) == before
        assert not polls.daily_post_suppressed(123, store.data.tournament.id, before[0])
    asyncio.run(scenario())


def test_admin_delete_handles_already_missing_post_and_suppression_is_per_day(context):
    from services import polls
    manager, store, _, current, message = context
    message.delete = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing"))

    async def scenario():
        await manager.tick()
        deleted_day = await manager.delete_daily_post()
        assert polls.daily_post_suppressed(123, store.data.tournament.id, deleted_day)
        assert not polls.daily_post_suppressed(999, store.data.tournament.id, deleted_day)
        assert not polls.daily_post_suppressed(123, "other-tournament", deleted_day)
        future = replace(store.data.matches[0], id="later", start_time=current[0] + timedelta(days=2))
        store.data = replace(store.data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id)
                                                for match in store.data.matches] + [future])
        current[0] = future.start_time - timedelta(hours=12)
        store.fetched_at = current[0]
        await manager.tick()
        latest = polls.get_latest_daily_post(123, store.data.tournament.id)
        assert latest is not None and latest[0] != deleted_day
    asyncio.run(scenario())


def test_first_timer_cannot_vote_on_daily_or_restored_buttons_until_mysetup(context):
    from services.voting import ScheduleVoteButton
    from services.polls import get_user_votes
    manager, store, channel, _, _ = context
    manager.client.voting_manager = manager
    def caller():
        return SimpleNamespace(guild_id=123, client=manager.client, extras={},
                               type=discord.InteractionType.component,
                               user=SimpleNamespace(id=999, display_name="New voter"),
                               response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))
    async def scenario():
        await manager.tick()
        button = next(item for item in channel.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button))
        first = caller()
        await button.callback(first)
        assert "Run `/mysetup`" in rendered_message(first.response.send_message.call_args).description
        first.response.defer.assert_not_awaited()
        assert get_user_votes(123, store.data.tournament.id, 999) == []
        poll = get_polls(123, store.data.tournament.id)[0]
        dynamic = ScheduleVoteButton(manager, poll, 1)
        restored = await ScheduleVoteButton.from_custom_id(first, dynamic.item, dynamic.template.fullmatch(dynamic.custom_id))
        assert not await restored.interaction_check(caller())
        user_settings.mark_setup_completed(123, 999)
        await restored.callback(caller())
        assert get_user_votes(123, store.data.tournament.id, 999)[0][1] == poll.team1_id
    asyncio.run(scenario())


def test_daily_post_waits_until_exact_24_hour_boundary_even_after_previous_day_ends(context):
    manager, store, channel, current, _ = context
    first = store.data.matches[0]
    previous = replace(first, id="previous-day", start_time=first.start_time - timedelta(days=3),
                       status=MatchStatus.COMPLETED, winner_id=first.team1_id)
    store.data = replace(store.data, matches=[previous, first])
    async def scenario():
        current[0] = first.start_time - timedelta(hours=24, seconds=1)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        current[0] += timedelta(seconds=1)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_previous_day_blocks_eight_hour_target_but_late_confirmation_posts_immediately(context):
    manager, store, channel, current, _ = context
    first = store.data.matches[0]
    previous = replace(first, id="previous-day", start_time=first.start_time - timedelta(days=1), status=MatchStatus.LIVE)
    store.data = replace(store.data, matches=[previous, first])
    async def scenario():
        current[0] = first.start_time - timedelta(hours=8, minutes=5)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        current[0] = first.start_time - timedelta(hours=2)
        store.fetched_at = current[0]
        store.data = replace(store.data, matches=[replace(previous, status=MatchStatus.COMPLETED, winner_id=previous.team1_id), first])
        await manager.tick()
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_daily_role_ping_is_only_allowed_on_first_publication(context):
    manager, store, channel, current, message = context
    set_worlds_role(123, 987)
    async def scenario():
        await manager.tick()
        first = channel.send.call_args
        assert "<@&987>" in str(first.kwargs["view"].to_components())
        assert first.kwargs["allowed_mentions"].to_dict() == {"roles": [987], "parse": []}
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        channel.send.assert_awaited_once()
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    asyncio.run(scenario())


def test_daily_prediction_test_never_pings_worlds_role(context):
    manager, _, channel, _, _ = context
    set_worlds_role(123, 987)
    manager.test_mode = True
    asyncio.run(manager.tick())
    call = channel.send.call_args
    assert "<@&987>" not in str(call.kwargs["view"].to_components())
    assert call.kwargs["allowed_mentions"].to_dict()["parse"] == []


def test_simulated_daily_buttons_show_the_visible_team_names(context, monkeypatch):
    from services import spoilers
    from services.spoiler_delivery import render_payload
    from services.spoilers import SpoilerPolicy
    from services.voting import PersonalVoteView
    manager, store, channel, current, _ = context
    live_data = store.data
    monkeypatch.setattr(spoilers, "_current", SpoilerPolicy(live_data, clock=lambda: current[0]))
    store.data = replace(live_data, matches=[
        replace(match, id=f"prediction-test:example:{index}")
        for index, match in enumerate(live_data.matches[:2])
    ])
    manager.test_mode = True

    async def scenario():
        await manager.tick()
        view = channel.send.call_args.kwargs["view"]
        result = render_payload({"components": view.to_components()})
        assert "[see spoiler]" not in str(result)
        assert "⟦" not in str(result)
        card, = view.children
        match_blocks = [child.content for child in card.children
                        if isinstance(child, discord.ui.TextDisplay) and " vs " in child.content]
        assert len(match_blocks) == 2
        assert not match_blocks[0].startswith("\u00a0\n\n")
        assert match_blocks[1].startswith("\u00a0\n\n## ")
        for index, child in enumerate(card.children):
            if isinstance(child, discord.ui.TextDisplay) and child.content.startswith("\u00a0\n\n"):
                spacer = card.children[index - 1]
                assert isinstance(spacer, discord.ui.Separator)
                assert spacer.spacing == discord.SeparatorSpacing.large
                assert spacer.visible is False
        for index, child in enumerate(card.children):
            if isinstance(child, discord.ui.TextDisplay) and child.content in match_blocks:
                assert isinstance(card.children[index + 1], discord.ui.ActionRow)
                assert "Bo5" in card.children[index + 2].content
        assert view.total_children_count <= 40
        buttons = [child for child in view.walk_children() if isinstance(child, discord.ui.Button)]
        for button in buttons:
            rendered = render_payload({"components": [button.to_component_dict()]})["components"][0]
            assert rendered["label"].strip("\xa0") == button.team_id
        poll = get_polls(123, store.data.tournament.id)[0]
        selected = PersonalVoteView(manager, poll, 7, selected_team=poll.team1_id)
        result = render_payload({"components": selected.to_components()})
        assert "[see spoiler]" not in str(result)
        assert "✓" in str(result)
    asyncio.run(scenario())


def test_daily_post_pings_matching_followers_once_and_excludes_role_members(context):
    manager, store, channel, current, _ = context
    set_worlds_role(123, 987)
    team = store.data.matches[0].team1_id
    for user_id in (1, 3, 4):
        user_settings.set_followed_team_ids(user_id, {team})
    user_settings.set_followed_team_ids(2, {"T1"})
    user_settings.set_server_ping(3, False)
    channel.guild.get_member = lambda user_id: SimpleNamespace(roles=[SimpleNamespace(id=987)] if user_id == 4 else [])
    async def scenario():
        await manager.tick()
        call = channel.send.call_args
        assert call.kwargs["allowed_mentions"].to_dict() == {"roles": [987], "users": [1], "parse": []}
        text = str(call.kwargs["view"].to_components())
        assert "<@1>" in text and "<@4>" not in text and "<@2>" not in text and "<@3>" not in text
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_later_confirmed_match_notifies_team_followers_without_duplicate_on_restart(context):
    manager, store, channel, current, message = context
    first, second = store.data.matches[:2]
    user_settings.set_followed_team_ids(5, {second.team1_id})
    store.data = replace(store.data, matches=[first, replace(second, team1_id="TBD")])
    async def scenario():
        await manager.tick()
        channel.send.assert_awaited_once()
        store.data = replace(store.data, matches=[first, second])
        await manager.tick()
        assert channel.send.await_count == 2
        call = channel.send.call_args
        assert call.kwargs["allowed_mentions"].to_dict() == {"users": [5], "parse": []}
        text = str(call.kwargs["view"].to_components())
        assert "<@5>" in text and "Voting Is Open for Your Teams" in text
        assert "https://discord.com/channels/123/456/789" in text
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        assert channel.send.await_count == 2
    asyncio.run(scenario())


def test_each_prediction_closes_at_its_five_minute_reminder_and_scores_once(context):
    manager, store, channel, current, message = context
    match = store.data.matches[0]
    async def scenario():
        await manager.tick()
        await manager.vote(match.id, 7, "C9")
        await manager.vote(match.id, 8, "KC")
        current[0] = match.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        await manager.tick()
        assert get_polls(123, store.data.tournament.id)[0].closed
        buttons = [item for item in message.edit.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
        assert all(button.disabled for button in buttons[:2])
        assert all(not button.disabled for button in buttons[2:])
        with pytest.raises(ValueError, match="closed"):
            await manager.vote(match.id, 7, "KC")
        store.data = replace(store.data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id="C9")])
        for _ in range(2):
            await manager.tick()
        assert get_leaderboard(123, store.data.tournament.id) == [(7, 1, 1), (8, 0, 1)]
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_five_minute_reminder_reveals_results_instead_of_linking_to_an_open_poll(context):
    manager, store, channel, current, _ = context
    async def scenario():
        await manager.tick()
        await manager.vote(store.data.matches[0].id, 7, "C9", "Lua")
        await manager.vote(store.data.matches[0].id, 8, "C9", "Jerry")
        await manager.vote(store.data.matches[0].id, 9, "KC", "Chris")
        with pytest.raises(ValueError, match="hidden"):
            get_poll_results(123, store.data.matches[0].id)
        current[0] = store.data.matches[0].start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        delivery = ServerReminderDelivery(manager.client, store, 123, clock=lambda: current[0], load_users=lambda: [], poll_manager=manager)
        assert await delivery.deliver(store.data.matches[0].id)
        assert channel.send.await_count == 2
        call = channel.send.call_args
        card = rendered_message(call)
        content = card.description
        assert call.kwargs["content"] is None
        assert call.kwargs["embeds"] == []
        assert "🕒 UPCOMING in 5 minutes" in content
        assert "### __Voting Results__" in content
        assert "### **C9**\n67%\n(2 votes)\n\n@Lua, @Jerry" in content
        assert "### **KC**\n33%\n(1 vote)\n\n@Chris" in content
        assert "-# • *Use `/votehistory`" in content
        assert content.endswith("prediction standings.*")
        assert "<@" not in content
    asyncio.run(scenario())


def test_stale_source_does_not_accept_votes_or_open_polls(context):
    manager, store, channel, _, _ = context
    store.last_error = "offline"
    async def scenario():
        await manager.tick()
        channel.send.assert_not_awaited()
        with pytest.raises(ValueError, match="unavailable"):
            await manager.vote(store.data.matches[0].id, 7, "C9")
    asyncio.run(scenario())


def test_poll_receipt_failure_does_not_repeat_send_in_running_process(context, monkeypatch):
    manager, store, channel, _, _ = context
    from services.polls import save_daily_post
    attempts = 0
    def save(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("locked")
        save_daily_post(*args)
    monkeypatch.setattr("services.voting.polls.save_daily_post", save)
    async def scenario():
        await manager.tick()
        current = manager.clock() + timedelta(seconds=60)
        manager.clock = lambda: current
        store.fetched_at = current
        await manager.tick()
        channel.send.assert_awaited_once()
        assert get_daily_post(123, store.data.tournament.id, "2026-10-15")[:2] == (456, 789)
    asyncio.run(scenario())


def test_eight_match_swiss_day_uses_one_paginated_post_with_buttons_under_matchups(context):
    manager, store, channel, current, _ = context
    first = store.data.matches[0].start_time.replace(day=23, hour=17)
    store.data = replace(store.data, matches=[
        replace(store.data.matches[0], id=f"swiss-{i}", start_time=first + timedelta(hours=i)) for i in range(8)
    ])
    current[0] = first - timedelta(hours=12)
    store.fetched_at = current[0]
    asyncio.run(manager.tick())
    channel.send.assert_awaited_once()
    view = channel.send.call_args.kwargs["view"]
    assert len([item for item in view.walk_children() if isinstance(item, discord.ui.Button)]) == 10
    assert len(manager.match_days()) == 1
    card, = view.children
    for i, item in enumerate(card.children):
        if isinstance(item, discord.ui.TextDisplay) and " vs " in item.content:
            assert isinstance(card.children[i + 1], discord.ui.ActionRow)
            assert "Bo5" in card.children[i + 2].content
    assert view.total_children_count <= 40
    assert "Page 1/2" in card.children[0].content
    manager.client.voting_manager = manager

    async def turn_page():
        from services.voting import DailyPageButton
        next_page = next(item for item in view.walk_children()
                         if isinstance(item, DailyPageButton) and item.item.label == "Next Matches")
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7), client=manager.client,
                                      extras={}, response=SimpleNamespace(defer=AsyncMock()))
        restored = await DailyPageButton.from_custom_id(interaction, next_page.item,
                                                        next_page.template.fullmatch(next_page.custom_id))
        await restored.callback(interaction)
        updated = channel.get_partial_message.return_value.edit.call_args.kwargs["view"]
        assert "Page 2/2" in updated.children[0].children[0].content
        assert updated.total_children_count <= 40
        assert len([item for item in updated.walk_children() if isinstance(item, discord.ui.Button)]) == 6
        channel.send.assert_awaited_once()
        assert channel.get_partial_message.return_value.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        await manager.tick()
        assert manager._daily_pages["2026-10-23"] == 1
    asyncio.run(turn_page())


def test_public_participation_updates_without_revealing_the_pick(context):
    manager, store, channel, _, message = context
    async def scenario():
        await manager.tick()
        match = store.data.matches[0]
        assert not await manager.vote(match.id, 7, "C9", "Lua")
        assert await manager.vote(match.id, 7, "KC", "Lua")
        await manager.tick()
        channel.send.assert_awaited_once()
        message.edit.assert_awaited_once()
        updated = str(message.edit.call_args.kwargs["view"].to_components())
        assert "@Lua" in updated and "These users cast a vote" in updated
        assert "Your pick" not in updated and "67%" not in updated
    asyncio.run(scenario())


def test_private_vote_confirmation_and_changed_confirmation(context):
    manager, store, channel, _, _ = context
    async def scenario():
        await manager.tick()
        buttons = [item for item in channel.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
        interaction = SimpleNamespace(
            guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        await buttons[0].callback(interaction)
        date = "Thu, 15 Oct 2026"
        embed = rendered_message(interaction.followup.send.call_args)
        assert embed.description.startswith(f"Your prediction for **C9** vs **KC** on {date} (Play-ins):\n## C9")
        assert "-# • *Use `/vote` to change your prediction until 5 minutes" in embed.description
        assert "Use `/votehistory`" in embed.description
        assert "Use `/leaderboard`" in embed.description
        assert interaction.followup.send.call_args.kwargs["view"].children[0].accent_colour.value == 0x5865F2
        assert not interaction.followup.send.call_args.args
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        await buttons[1].callback(interaction)
        embed = rendered_message(interaction.followup.send.call_args)
        assert embed.description.startswith(f"Your prediction for **C9** vs **KC** on {date} (Play-ins):\n## KC")
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        assert interaction.followup.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert channel.send.await_count == 3  # Board plus two participation messages.
    asyncio.run(scenario())


def test_vote_refreshes_public_board_before_confirmation_or_announcement(context):
    manager, store, channel, now, message = context

    async def scenario():
        await manager.tick()
        button = next(item for item in channel.send.call_args.kwargs["view"].walk_children()
                      if isinstance(item, discord.ui.Button))
        # A previous transient board failure must not postpone a new vote's update.
        day = next(iter(manager._posted))
        manager._retry_at[day] = now[0] + timedelta(seconds=60)
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()

        async def slow_confirmation(**kwargs):
            confirmation_started.set()
            await release_confirmation.wait()

        interaction = SimpleNamespace(
            guild_id=123, user=SimpleNamespace(id=8, display_name="Miwa"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(side_effect=slow_confirmation)),
        )
        task = asyncio.create_task(button.callback(interaction))
        try:
            await asyncio.wait_for(confirmation_started.wait(), timeout=5)
            message.edit.assert_awaited_once()
            components = str(message.edit.call_args.kwargs["view"].to_components())
            assert "@Miwa" in components
            assert channel.send.await_count == 1  # Announcement hasn't started.
            assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        finally:
            release_confirmation.set()
            await task
        assert channel.send.await_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("selected_index", [0, 1])
def test_successful_private_confirmation_reveals_all_teams_even_when_pick_was_hidden(context, monkeypatch, selected_index):
    import json
    from services import spoilers
    from services.spoiler_delivery import render_payload
    from services.team_emojis import configure_team_emojis, team_emoji

    manager, store, channel, current, _ = context
    match = store.data.matches[0]
    prior = replace(match, id="prerequisite", start_time=match.start_time - timedelta(days=1),
                    status=MatchStatus.COMPLETED, winner_id=match.team2_id, team1_score=1, team2_score=3)
    policy = spoilers.SpoilerPolicy(replace(store.data, matches=[prior, *store.data.matches]),
                                   ended_at={prior.id: current[0]}, dependencies={(match.id, 2): [prior.id]},
                                   clock=manager.clock)
    monkeypatch.setattr(spoilers, "_current", policy)
    configure_team_emojis([discord.PartialEmoji(name="CLOUD9", id=111), discord.PartialEmoji(name="KC", id=222),
                           discord.PartialEmoji(name="beeQuestion", id=333)], store.data.teams)

    async def scenario():
        assert policy.participant_protected(match, 2)
        await manager.tick()
        buttons = [item for item in channel.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
        public = render_payload({"components": channel.send.call_args.kwargs["view"].to_components()})
        assert "beeQuestion" in json.dumps(public)
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await buttons[selected_index].callback(interaction)
        reply = interaction.followup.send.call_args.kwargs
        assert reply["ephemeral"] is True
        private = json.dumps(render_payload({"components": reply["view"].to_components()}), ensure_ascii=False)
        assert "||" not in private and "beeQuestion" not in private
        assert policy.team(match.team1_id, emoji=team_emoji(match.team1_id)) in private
        assert policy.team(match.team2_id, emoji=team_emoji(match.team2_id)) in private
        chosen = match.team1_id if selected_index == 0 else match.team2_id
        assert "## " + policy.team(chosen, emoji=team_emoji(chosen)) in private
        assert policy.participant_protected(match, 2)  # No public reveal side effect.
        announcement = channel.send.call_args.kwargs
        assert announcement["allowed_mentions"].to_dict() == {"parse": []}
        public_notice = json.dumps(render_payload({"components": announcement["view"].to_components()}), ensure_ascii=False)
        assert "@Lua" in public_notice and "just cast a vote on:" in public_notice
        assert "\\n\\n## " in public_notice
        assert "||" in public_notice  # Protected participant stays protected.
        assert "Your prediction" not in public_notice and "Your pick" not in public_notice

    try:
        asyncio.run(scenario())
    finally:
        configure_team_emojis(())


def test_announcement_failure_does_not_undo_vote_or_private_confirmation(context):
    from services.polls import get_user_votes
    manager, store, channel, _, _ = context

    async def scenario():
        await manager.tick()
        button = next(item for item in channel.send.call_args.kwargs["view"].walk_children()
                      if isinstance(item, discord.ui.Button))
        channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await button.callback(interaction)
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == button.team_id
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        assert "Your prediction" in rendered_message(interaction.followup.send.call_args).description
    asyncio.run(scenario())


def test_announcement_skips_unconfigured_channels(context):
    manager, store, channel, _, _ = context
    with user_settings.get_connection() as connection:
        connection.execute("DELETE FROM server_settings WHERE guild_id = 123")
    asyncio.run(manager.announce_vote(store.data.matches[0].id, "Lua"))
    channel.send.assert_not_awaited()


def test_simulated_vote_announcements_are_labelled_and_never_ping(context):
    manager, store, channel, _, _ = context
    manager.test_mode = True
    asyncio.run(manager.announce_vote(store.data.matches[0].id, "Lua"))
    channel.send.assert_awaited_once()
    sent = channel.send.call_args
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    text = rendered_message(sent).description
    assert "TEST" in text and "@Lua" in text and "just cast a vote on:\n\n## " in text
    assert "(Thu, 15 Oct 2026)" in text
    assert "You have 11 hours and 55 minutes to vote." in text
    assert "Use `/vote`" in text and "`/vote`" in text
    assert "C9" in text and "KC" in text


@pytest.mark.parametrize("minutes,expected", [(130, "2 hours and 5 minutes"),
                                            (6, "1 minute"), (5.5, "less than a minute"), (5, None)])
def test_announcement_voting_time_uses_five_minute_cutoff(context, minutes, expected):
    manager, store, channel, now, _ = context
    match = store.data.matches[0]
    now[0] = match.start_time - timedelta(minutes=minutes)
    asyncio.run(manager.announce_vote(match.id, "Lua"))
    text = rendered_message(channel.send.call_args).description
    if expected:
        assert f"You have {expected} to vote." in text
        assert "`/vote`" in text and "`/vote`" in text
    else:
        assert "Voting for this match has now closed." in text
        assert "You have" not in text and "`/vote`" not in text


def test_failed_vote_does_not_send_participation_message(context):
    manager, store, channel, now, _ = context

    async def scenario():
        await manager.tick()
        button = next(item for item in channel.send.call_args.kwargs["view"].walk_children()
                      if isinstance(item, discord.ui.Button))
        now[0] = store.data.matches[0].start_time
        store.fetched_at = now[0]
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await button.callback(interaction)
        channel.send.assert_awaited_once()
        assert "closed" in rendered_message(interaction.followup.send.call_args).description.lower()
    asyncio.run(scenario())


def test_daily_post_shows_registered_zones_but_confirmation_keeps_single_date(context):
    manager, store, channel, _, message = context
    for uid, zone in [(1, "Europe/London"), (2, "Europe/Berlin"), (3, "Asia/Tokyo")]:
        user_settings.set_user_timezone(uid, zone)
    async def scenario():
        await manager.tick()
        components = str(channel.send.call_args.kwargs["view"].to_components())
        for label in ("London", "Berlin", "Tokyo", "19:00", "20:00", "3:00"):
            assert label in components
        buttons = [item for item in channel.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button)]
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await buttons[0].callback(interaction)
        text = rendered_message(interaction.followup.send.call_args).description
        assert "on Thu, 15 Oct 2026 (Play-ins):" in text
        assert all(city not in text for city in ("London", "Berlin", "Tokyo"))
        assert "19:00" not in text and "20:00" not in text and "3:00" not in text
        user_settings.set_user_timezone(3, "Asia/Seoul")
        await manager.tick()
        updated = str(message.edit.call_args.kwargs["view"].to_components())
        assert "Seoul" in updated and "Tokyo" not in updated
        assert channel.send.await_count == 2  # Board plus participation message.
    asyncio.run(scenario())


def test_vote_shortcut_targets_last_pick_then_next_open_match_and_enforces_cutoff(context):
    from services.voting import PersonalVoteView
    from services.polls import get_user_votes
    manager, store, _, current, _ = context
    first, second = store.data.matches[:2]
    async def scenario():
        await manager.tick()
        assert (await manager.personal_vote_target(7)).match_id == first.id
        await manager.vote(second.id, 7, second.team1_id, "Lua")
        current[0] += timedelta(seconds=1)
        assert (await manager.personal_vote_target(7)).match_id == second.id
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        poll = await manager.personal_vote_target(7)
        assert poll.match_id == first.id
        view = PersonalVoteView(manager, poll, 7)
        other = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await view.interaction_check(other)
        assert other.response.send_message.call_args.kwargs["ephemeral"] is True
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await view.children[1].callback(interaction)
        assert dict((p.match_id, team) for p, team in get_user_votes(123, store.data.tournament.id, 7))[first.id] == first.team2_id
        current[0] = first.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        await view.children[0].callback(interaction)
        assert "closed" in rendered_message(interaction.followup.send.call_args).description
        assert (await manager.personal_vote_target(7)).match_id == second.id
        current[0] = second.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        assert await manager.personal_vote_target(7) is None
        store.last_error = "offline"
        with pytest.raises(ValueError, match="unavailable"):
            await manager.personal_vote_target(7)
    asyncio.run(scenario())


@pytest.mark.parametrize("history", [False, True])
def test_all_open_votes_can_be_changed_independently_and_selections_update(context, history):
    from services.polls import get_user_votes
    from services.voting import personal_vote_payloads, my_vote_payloads
    manager, store, _, current, _ = context
    first, second = store.data.matches[:2]
    async def scenario():
        await manager.tick()
        for match in (first, second):
            await manager.vote(match.id, 7, match.team1_id, "Lua")
        available = await manager.personal_vote_targets(7)
        assert [poll.match_id for poll in available] == [first.id, second.id]
        saved = get_user_votes(123, store.data.tournament.id, 7)
        if history:
            payload, = my_vote_payloads(manager, saved, available, 7, store.data, now=manager.clock(),
                                        timezone_name="Europe/Berlin", time_format="12h", display_name="Lua")
        else:
            payload, = personal_vote_payloads(manager, available, 7, saved, timezone_name="Europe/Berlin", time_format="12h")
        view = payload["view"]
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 4
        assert [button.style for button in buttons] == [discord.ButtonStyle.success, discord.ButtonStyle.primary] * 2
        other = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await view.interaction_check(other)
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()), edit_original_response=AsyncMock())
        await buttons[1].callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
        interaction.message.edit.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once()
        assert interaction.edit_original_response.call_args.kwargs["view"] is view
        assert interaction.edit_original_response.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert buttons[0].style == discord.ButtonStyle.primary and buttons[1].style == discord.ButtonStyle.success
        assert buttons[2].style == discord.ButtonStyle.success  # Other match is untouched.
        choices = {poll.match_id: team for poll, team in get_user_votes(123, store.data.tournament.id, 7)}
        assert choices == {first.id: first.team2_id, second.id: second.team1_id}
        if history:
            text = rendered_payload(payload).description
            assert f"Your pick: **{first.team2_id}**" in text
        current[0] = first.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        assert [poll.match_id for poll in await manager.personal_vote_targets(7)] == [second.id]
        await buttons[0].callback(interaction)
        assert "closed" in rendered_message(interaction.followup.send.call_args).description
        assert {poll.match_id: team for poll, team in get_user_votes(123, store.data.tournament.id, 7)} == choices
        if history:
            payload, = my_vote_payloads(manager, get_user_votes(123, store.data.tournament.id, 7),
                                        await manager.personal_vote_targets(7), 7, store.data, now=manager.clock())
            remaining = [item for item in payload["view"].walk_children() if isinstance(item, discord.ui.Button)]
            assert len(remaining) == 2
            assert all(button.poll.match_id == second.id for button in remaining)
    asyncio.run(scenario())


def test_many_private_predictions_are_split_without_losing_matches(context):
    from services.polls import Poll
    from services.voting import personal_vote_payloads
    manager, store, _, _, _ = context
    first = store.data.matches[0]
    matches = [replace(first, id=f"private-{index}", start_time=first.start_time + timedelta(minutes=index)) for index in range(12)]
    store.data = replace(store.data, matches=matches)
    available = [Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
                 for match in matches]
    async def scenario():
        pages = personal_vote_payloads(manager, available, 7, [(poll, poll.team1_id) for poll in available])
        assert len(pages) == 2
        buttons = [item for page in pages for item in page["view"].walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 24
        assert {button.poll.match_id for button in buttons} == {match.id for match in matches}
        for page in pages:
            view = page["view"]
            assert view.total_children_count <= 40
            texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
            assert sum(map(len, texts)) <= 4000
            assert sum("Your Currently Active Predictions" in text for text in texts) == 1
    asyncio.run(scenario())


def test_myvotes_buttons_preserve_history_and_paginate_within_discord_limits(context):
    from services.polls import Poll
    from services.voting import my_vote_payloads
    manager, store, _, _, _ = context
    first = store.data.matches[0]
    matches = [replace(first, id=f"history-{index}", start_time=first.start_time + timedelta(minutes=index)) for index in range(12)]
    past = replace(first, id="finished-history", status=MatchStatus.COMPLETED,
                   start_time=manager.clock() - timedelta(days=1), winner_id=first.team1_id,
                   team1_score=3, team2_score=0)
    store.data = replace(store.data, matches=matches + [past])
    available = [Poll(123, match.id, match.tournament_id, match.team1_id, match.team2_id, match.start_time, False, None)
                 for match in matches]
    finished = Poll(123, past.id, past.tournament_id, past.team1_id, past.team2_id, past.start_time, True, past.winner_id)
    rows = [(poll, poll.team1_id) for poll in available] + [(finished, past.team2_id)]

    async def scenario():
        pages = list(my_vote_payloads(manager, rows, available, 7, store.data, now=manager.clock()))
        assert len(pages) == 3
        buttons = [item for page in pages for item in page["view"].walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 24
        assert {button.poll.match_id for button in buttons} == {match.id for match in matches}
        history = rendered_payload(pages[0]).description
        assert "Past Games" in history and "Incorrect" in history
        assert f"Result: ||**__{past.team1_id}__** **3–0** **{past.team2_id}**||" in history
        for page in pages:
            view = page["view"]
            assert view.total_children_count <= 40
            assert sum(len(item.content) for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)) <= 4000
    asyncio.run(scenario())


def test_today_lookup_publishes_reusable_polls_and_private_vote_confirmations(context):
    from test_bot_commands import day_callback
    from services.polls import get_published_match_ids, get_user_votes
    from services.voting import VoteButton, personal_vote_payloads
    from services.vote_panel import OpenVotePanel
    manager, store, channel, current, _ = context
    current[0] = store.data.matches[0].start_time - timedelta(hours=1)
    store.fetched_at = current[0]
    manager.client.voting_manager = manager
    interaction = SimpleNamespace(guild_id=123, client=manager.client,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    async def scenario():
        callback = day_callback(store.data, current[0])
        await callback(interaction, 0)
        interaction.response.defer.assert_awaited_once_with(ephemeral=False)
        interaction.followup.send.assert_awaited_once()
        payload = interaction.followup.send.call_args.kwargs
        assert payload["ephemeral"] is False and payload["allowed_mentions"].to_dict() == {"parse": []}
        text = rendered_message(interaction.followup.send.call_args).description
        assert "Berlin" in text and "London" in text and "Tokyo" in text
        assert "Use `/vote`" in text and "Use `/votehistory`" in text and "Use `/leaderboard`" in text
        assert "%" not in text and "votes)" not in text
        view = payload["view"]
        assert view.is_persistent() and view.total_children_count <= 40
        assert sum(isinstance(item, OpenVotePanel) for item in view.walk_children()) == 1
        available = await manager.personal_vote_targets(7)
        private = personal_vote_payloads(manager, available, 7, [])
        buttons = [item for item in private[0]["view"].walk_children() if isinstance(item, VoteButton)]
        assert len(buttons) == 4
        assert len(get_polls(123, store.data.tournament.id)) == 2
        assert get_published_match_ids(123, store.data.tournament.id) == {match.id for match in store.data.matches}
        channel.send.assert_not_awaited()  # No extra board or reminder was posted.
        vote = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await buttons[0].callback(vote)
        assert vote.followup.send.call_args.kwargs["ephemeral"] is True
        await callback(interaction, 0)
        await buttons[1].callback(vote)
        choices = get_user_votes(123, store.data.tournament.id, 7)
        assert len(choices) == 1 and choices[0][1] == store.data.matches[0].team2_id
        assert len(get_polls(123, store.data.tournament.id)) == 2
    asyncio.run(scenario())


def test_today_failed_send_is_not_counted_as_published_opportunity(context):
    from test_bot_commands import day_callback
    from services.polls import get_published_match_ids
    manager, store, _, current, _ = context
    current[0] = store.data.matches[0].start_time - timedelta(hours=1)
    store.fetched_at = current[0]
    manager.client.voting_manager = manager
    interaction = SimpleNamespace(guild_id=123, client=manager.client,
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No permissions"))))
    async def scenario():
        with pytest.raises(discord.Forbidden):
            await day_callback(store.data, current[0])(interaction, 0)
        assert get_published_match_ids(123, store.data.tournament.id) == set()
    asyncio.run(scenario())


def test_today_voting_excludes_ineligible_matches_and_pages_within_discord_limits(context):
    from services.voting import schedule_vote_payloads, ScheduleVoteButton
    from services.polls import get_published_match_ids
    manager, store, _, current, _ = context
    template = store.data.matches[0]
    current[0] = template.start_time - timedelta(minutes=6)
    store.fetched_at = current[0]
    eligible = [replace(template, id=f"schedule-{index}", start_time=template.start_time + timedelta(minutes=index)) for index in range(12)]
    excluded = [replace(template, id="live", status=MatchStatus.LIVE),
                replace(template, id="done", status=MatchStatus.COMPLETED),
                replace(template, id="postponed", status=MatchStatus.POSTPONED),
                replace(template, id="unknown", team1_id="TBD", team2_id="TBD"),
                replace(template, id="late", start_time=current[0] + timedelta(minutes=4))]
    store.data = replace(store.data, matches=eligible + excluded)
    async def scenario():
        prepared = await manager.prepare_schedule_polls(store.data.matches)
        assert set(prepared) == {match.id for match in eligible}
        entries = [(match, f"## {match.team1_id} vs {match.team2_id}\n" + "Details " * 40) for match in store.data.matches]
        pages = schedule_vote_payloads(manager, "__Worlds Today__", "__15 October 2026__", entries, prepared)
        assert len(pages) > 1
        all_buttons = [item for page in pages for item in page["view"].walk_children() if isinstance(item, ScheduleVoteButton)]
        assert len(all_buttons) == 24
        assert get_published_match_ids(123, store.data.tournament.id) == set()
        for page in pages:
            view = page["view"]
            assert view.total_children_count <= 40
            assert sum(len(item.content) for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)) <= 4000
        await manager.acknowledge_schedule_opportunities(pages[0]["view"].matches, prepared)
        assert get_published_match_ids(123, store.data.tournament.id) == {match.id for match in pages[0]["view"].matches if match.id in prepared}
        store.last_error = "offline"
        assert await manager.prepare_schedule_polls(store.data.matches) == {}
    asyncio.run(scenario())


def test_schedule_buttons_restore_after_restart_and_reject_changed_matchup_or_late_click(context):
    from services.voting import ScheduleVoteButton
    from services.polls import get_user_votes
    manager, store, _, current, _ = context
    first = store.data.matches[0]
    async def scenario():
        prepared = await manager.prepare_schedule_polls(store.data.matches)
        button = ScheduleVoteButton(manager, prepared[first.id], 1)
        restarted = VotingManager(manager.client, store, 123, clock=manager.clock)
        manager.client.voting_manager = restarted
        interaction = SimpleNamespace(guild_id=123, client=manager.client, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        restored = await ScheduleVoteButton.from_custom_id(interaction, button.item, button.template.fullmatch(button.custom_id))
        await restored.callback(interaction)
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == first.team1_id
        store.data = replace(store.data, matches=[replace(first, team2_id="FUR"), store.data.matches[1]])
        await restored.callback(interaction)
        assert "matchup changed" in rendered_message(interaction.followup.send.call_args).description.lower()
        current[0] = first.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        await restored.callback(interaction)
        assert "closed" in rendered_message(interaction.followup.send.call_args).description
        other = SimpleNamespace(guild_id=456, response=SimpleNamespace(send_message=AsyncMock()))
        await restored.callback(other)
        assert other.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(scenario())


def test_controlled_prediction_day_completes_without_affecting_real_leaderboard(context, monkeypatch):
    manager, store, channel, current, _ = context
    from services.prediction_test import start_prediction_test
    from services.delivery_records import get_delivery_record
    tasks = []
    background = SimpleNamespace(store=store, prediction_test_running=False, manage_test_task=tasks.append)
    manager.client.background_polling = background
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz or UTC)
    voted = False
    async def advance(seconds):
        nonlocal voted
        if not voted:
            test_manager = manager.client.prediction_test_manager
            for index, match in enumerate(test_manager.store.data.matches):
                await test_manager.vote(match.id, 7, match.team1_id if index == 0 else match.team2_id, "lua")
            voted = True
        current[0] += timedelta(seconds=60)
    monkeypatch.setattr("services.prediction_test.datetime", Clock)
    monkeypatch.setattr("services.prediction_test.asyncio.sleep", advance)
    interaction = SimpleNamespace(client=manager.client, user=SimpleNamespace(id=7, display_name="lua"),
                                  followup=SimpleNamespace(send=AsyncMock()))
    async def scenario():
        result = await start_prediction_test(interaction, 123)
        assert "Test daily post sent" in result
        await tasks[0]
        assert not background.prediction_test_running
        assert manager.client.prediction_test_manager is None
        assert channel.send.await_count == 5  # Board, two cutoff reminders, two completed-series results.
        assert get_leaderboard(123, store.data.tournament.id) == []
        assert get_delivery_record(123, store.data.matches[0].id) is None
        report, preview = interaction.followup.send.call_args_list
        assert "real Worlds leaderboard was not changed" in rendered_message(report).description
        card = rendered_message(preview)
        assert "TEST: @lua's Worlds Predictions" in card.title
        assert "Your pick: **C9** ||(Correct :KingPeepo:)||" in card.description
        assert "Your pick: **MVK** ||(Incorrect :peepoSad:)||" in card.description
        assert "Result: ||**__C9__** **3–0** **KC**||" in card.description
        assert "Result: ||**__FUR__** **3–0** **MVK**||" in card.description
        assert preview.kwargs["ephemeral"] is True
    asyncio.run(scenario())


def test_confirmed_pairing_updates_existing_daily_post_without_reposting(context):
    manager, store, channel, _, message = context
    original = store.data.matches[1]
    store.data = replace(store.data, matches=[store.data.matches[0], replace(original, team1_id="TBD", team2_id="TBD")])
    async def scenario():
        await manager.tick()
        view = channel.send.call_args.kwargs["view"]
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 2
        assert "TBD" not in str(view.to_components())
        await manager.vote(store.data.matches[0].id, 7, "C9", "Lua")
        store.data = replace(store.data, matches=[store.data.matches[0], original])
        await manager.tick()
        channel.send.assert_awaited_once()
        updated = message.edit.call_args.kwargs["view"]
        buttons = [item for item in updated.walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 4
        assert all(not button.disabled for button in buttons)
        from services.polls import get_user_votes
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == "C9"
    asyncio.run(scenario())


def test_swiss_four_new_pairings_preserve_original_votes_and_do_not_duplicate(context):
    from services.polls import get_published_match_ids, get_user_votes
    manager, store, channel, _, message = context
    base = store.data.matches[0]
    all_matches = [replace(base, id=f"swiss-{i}", start_time=base.start_time + timedelta(minutes=30*i)) for i in range(8)]
    store.data = replace(store.data, matches=all_matches[:4])
    async def scenario():
        await manager.tick()
        await manager.vote(all_matches[0].id, 7, base.team1_id, "Secret Friend")
        store.data = replace(store.data, matches=all_matches)
        await manager.tick()
        updated = message.edit.call_args.kwargs["view"]
        assert len([item for item in updated.walk_children() if isinstance(item, discord.ui.Button)]) == 10
        assert updated.total_children_count <= 40
        assert "@Secret Friend" in str(updated.to_components())
        assert len(get_polls(123, store.data.tournament.id)) == 8
        assert len(get_published_match_ids(123, store.data.tournament.id)) == 8
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == base.team1_id
        restarted = VotingManager(manager.client, store, 123, clock=manager.clock)
        await restarted.tick()
        channel.send.assert_awaited_once()
        assert len(get_polls(123, store.data.tournament.id)) == 8
    asyncio.run(scenario())


def test_match_discovered_at_cutoff_does_not_receive_an_open_poll(context):
    manager, store, channel, current, message = context
    first, second = store.data.matches
    store.data = replace(store.data, matches=[first])
    async def scenario():
        await manager.tick()
        current[0] = second.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        store.data = replace(store.data, matches=[first, second])
        await manager.tick()
        assert {p.match_id for p in get_polls(123, store.data.tournament.id)} == {first.id}
        assert all(item.disabled for item in message.edit.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button))
        assert await manager.personal_vote_target(7) is None
        channel.send.assert_awaited_once()
    asyncio.run(scenario())


def test_failed_discord_send_never_counts_as_a_published_opportunity(context):
    from services.polls import get_published_match_ids
    manager, store, channel, _, _ = context
    channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No access")
    async def scenario():
        await manager.tick()
        assert get_daily_post(123, store.data.tournament.id, "2026-10-15") is None
        assert get_published_match_ids(123, store.data.tournament.id) == set()
        assert await manager.personal_vote_target(7) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_deleted_daily_board_is_replaced_without_losing_votes(context, restart):
    from services.polls import get_user_votes
    manager, store, channel, current, message = context
    set_worlds_role(123, 987)
    async def scenario():
        await manager.tick()
        await manager.vote(store.data.matches[0].id, 7, "C9", "Lua")
        message.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        channel.send.return_value = SimpleNamespace(id=790)
        if restart:
            active = VotingManager(manager.client, store, 123, clock=manager.clock)
        else:
            current[0] += timedelta(minutes=5)
            store.fetched_at = current[0]
            active = manager
        await active.tick()
        assert channel.send.await_count == 2
        assert "<@&987>" in str(channel.send.call_args.kwargs["view"].to_components())
        assert channel.send.call_args.kwargs["allowed_mentions"].to_dict() == {"roles": [987], "parse": []}
        assert get_daily_post(123, store.data.tournament.id, "2026-10-15")[1] == 790
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == "C9"
        message.edit.side_effect = None  # The replacement message exists.
        await VotingManager(manager.client, store, 123, clock=manager.clock).tick()
        assert channel.send.await_count == 2
    asyncio.run(scenario())


def test_stale_button_cannot_vote_for_a_changed_matchup_even_if_team_remains(context):
    manager, store, channel, _, _ = context
    async def scenario():
        await manager.tick()
        button = next(item for item in channel.send.call_args.kwargs["view"].walk_children() if isinstance(item, discord.ui.Button))
        store.data = replace(store.data, matches=[replace(store.data.matches[0], team2_id="FUR"), store.data.matches[1]])
        interaction = SimpleNamespace(guild_id=123, user=SimpleNamespace(id=7, display_name="Lua"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await button.callback(interaction)
        assert "changed" in rendered_message(interaction.followup.send.call_args).description
        from services.polls import get_user_votes
        assert get_user_votes(123, store.data.tournament.id, 7) == []
    asyncio.run(scenario())


def test_locked_poll_never_reopens_when_riot_delays_the_start(context):
    manager, store, _, current, _ = context
    match = store.data.matches[0]
    async def scenario():
        await manager.tick()
        current[0] = match.start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        await manager.tick()
        store.data = replace(store.data, matches=[replace(m, start_time=m.start_time + timedelta(hours=1)) for m in store.data.matches])
        await manager.tick()
        assert next(p for p in get_polls(123, store.data.tournament.id) if p.match_id == match.id).closed
        with pytest.raises(ValueError, match="closed"):
            await manager.vote(match.id, 7, match.team1_id)
    asyncio.run(scenario())


def test_next_board_waits_for_previous_matchday_and_includes_mvp_on_publication(context):
    from services.polls import cast_vote
    manager, store, channel, current, message = context
    first, second = store.data.matches
    future = replace(first, id="future", start_time=first.start_time + timedelta(days=1))
    channel.send.side_effect = [SimpleNamespace(id=789), SimpleNamespace(id=790)]
    async def scenario():
        await manager.tick()
        for match in (first, second):
            await manager.vote(match.id, 7, match.team1_id, "Lua")
            await manager.vote(match.id, 8, match.team1_id, "Jerry")
        store.data = replace(store.data, matches=[first, second, future])
        await manager.tick()
        assert channel.send.await_count == 1
        assert "Previous Match-Day MVP" not in str(channel.send.call_args.kwargs["view"].to_components())
        current[0] = second.start_time + timedelta(hours=4)
        store.fetched_at = current[0]
        store.data = replace(store.data, matches=[replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id)
                                                if m.id != future.id else m for m in store.data.matches])
        await manager.tick()
        components = str(channel.send.call_args.kwargs["view"].to_components())
        assert "Previous Match-Day MVP" in components
        assert "Lua" in components and "2/2 correct" in components and "100.0% participation" in components
        assert "Jerry" in components
        assert "points" not in components
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert channel.send.await_count == 2
    asyncio.run(scenario())


def test_unknown_next_day_blocks_later_day_and_late_matchups_update_same_post(context):
    manager, store, channel, current, message = context
    first = store.data.matches[0]
    completed = replace(first, status=MatchStatus.COMPLETED, winner_id=first.team1_id)
    next_day = replace(first, id="day2", team1_id="TBD", start_time=first.start_time + timedelta(days=1))
    later = replace(first, id="day3", start_time=first.start_time + timedelta(days=2))
    store.data = replace(store.data, matches=[completed, next_day, later])
    channel.send.side_effect = [SimpleNamespace(id=790), SimpleNamespace(id=791)]
    async def scenario():
        current[0] = first.start_time + timedelta(hours=1)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        confirmed = replace(next_day, team1_id=first.team1_id)
        store.data = replace(store.data, matches=[completed, confirmed, later])
        await manager.tick()
        channel.send.assert_awaited_once()
        assert not any(p.match_id == later.id for p in get_polls(123, store.data.tournament.id))
        late_match = replace(confirmed, id="day2-late", start_time=confirmed.start_time + timedelta(hours=1))
        store.data = replace(store.data, matches=[completed, confirmed, late_match, later])
        await manager.tick()
        channel.send.assert_awaited_once()
        assert "day2-late" in str(message.edit.call_args.kwargs["view"].to_components())
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        store.data = replace(store.data, matches=[replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id)
                                                if m.id != later.id else m for m in store.data.matches])
        current[0] = later.start_time - timedelta(hours=24)
        store.fetched_at = current[0]
        await manager.tick()
        assert channel.send.await_count == 2
        assert any(p.match_id == later.id for p in get_polls(123, store.data.tournament.id))
    asyncio.run(scenario())


def test_publication_write_failure_retries_original_ack_time_after_cutoff(context, monkeypatch):
    from services.polls import get_published_match_ids, record_poll_opportunity
    manager, store, _, current, _ = context
    first = store.data.matches[0]
    current[0] = first.start_time - timedelta(minutes=6)
    store.fetched_at = current[0]
    writes_fail = [True]
    def save(*args):
        if writes_fail[0]:
            raise sqlite3.OperationalError("locked")
        record_poll_opportunity(*args)
    monkeypatch.setattr("services.voting.polls.record_poll_opportunity", save)
    async def scenario():
        await manager.tick()
        assert get_published_match_ids(123, store.data.tournament.id) == set()
        current[0] = first.start_time - timedelta(minutes=4)
        store.fetched_at = current[0]
        writes_fail[0] = False
        await manager.tick()
        assert first.id in get_published_match_ids(123, store.data.tournament.id)
        with user_settings.get_connection() as connection:
            published = connection.execute("SELECT published_at FROM match_polls WHERE match_id = ?", (first.id,)).fetchone()[0]
        assert datetime.fromisoformat(published) == first.start_time - timedelta(minutes=6)
    asyncio.run(scenario())


def test_reschedule_moves_match_between_boards_without_duplicate_poll(context):
    manager, store, channel, current, message = context
    first = store.data.matches[0]
    store.data = replace(store.data, matches=[first])
    channel.send.side_effect = [SimpleNamespace(id=789), SimpleNamespace(id=790)]
    async def scenario():
        await manager.tick()
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        store.data = replace(store.data, matches=[replace(first, start_time=first.start_time + timedelta(days=1))])
        await manager.tick()
        assert channel.send.await_count == 1
        current[0] = first.start_time
        store.fetched_at = current[0]
        await manager.tick()
        assert channel.send.await_count == 2
        old_view = message.edit.call_args.kwargs["view"]
        assert not any(isinstance(item, discord.ui.Button) for item in old_view.walk_children())
        assert len(get_polls(123, store.data.tournament.id)) == 1
        from services.polls import get_user_votes
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == first.team1_id
    asyncio.run(scenario())
