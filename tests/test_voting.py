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
from services.server_settings import initialize_server_settings, set_reminder_channel
from services.tournament_data import load_tournament_data
from services.voting import VotingManager


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "voting.db")
    user_settings.initialize_database()
    initialize_server_settings()
    initialize_delivery_records()
    initialize_polls()
    set_reminder_channel(123, 456)
    data = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    current = [data.matches[0].start_time - timedelta(hours=12)]
    store = SimpleNamespace(data=data, fetched_at=current[0], last_error=None)
    channel = Mock(spec=discord.TextChannel)
    channel.id = 456
    channel.type = discord.ChannelType.text
    channel.guild = SimpleNamespace(id=123, me=object())
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
        channel.send.assert_awaited_once()
        current[0] += timedelta(seconds=1)
        await manager.tick()
        await manager.tick()
        channel.send.assert_awaited_once()
        call = channel.send.call_args
        components = call.kwargs["view"].to_components()
        assert "Who wins?" in str(components)
        assert "C9 vs KC" in str(components)
        assert "FUR vs MVK" in str(components)
        card, = call.kwargs["view"].children
        assert isinstance(card, discord.ui.Container)
        assert card.accent_colour.value == 0x5865F2
        assert "Play-ins · Opening Round · Bo5" in str(components)
        assert "# __Worlds Predictions__" in str(components)
        assert "`/currentvotes`" in str(components)
        assert "Communism's Worlds prediction standings" in str(components)
        assert card.children[-1].content == (
            "-# Use `/changevote` to change your prediction until 5 minutes before a match.\n"
            "-# Use `/currentvotes` to see how everyone voted once voting closes.\n"
            "-# Use `/leaderboard` to see Communism's Worlds prediction standings."
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
        embed = rendered_message(channel.send.call_args)
        content = str(embed.to_dict())
        assert embed.colour.value == 0x5865F2
        assert embed.description == "-# Starts in 5 minutes!\n\n__Voting Results__"
        assert channel.send.call_args.args[0] is None
        assert embed.title == "C9 vs KC"
        assert embed.footer.text
        assert len(embed.fields) == 2
        assert all(field.inline for field in embed.fields[:2])
        assert embed.footer.text == (
            "Use /currentvotes to see how everyone voted once voting closes.\n"
            "Use /leaderboard to see Communism's Worlds prediction standings."
        )
        assert embed.fields[0].name == "**C9**"
        assert embed.fields[0].value == "67%\n(2 votes)\n\n@Lua, @Jerry"
        assert "@Lua, @Jerry" in content
        assert embed.fields[1].name == "**KC**"
        assert embed.fields[1].value == "33%\n(1 vote)\n\n@Chris"
        assert "Chris" in content
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


def test_eight_match_swiss_day_spanning_utc_midnight_is_one_interleaved_post(context):
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
    assert len([item for item in view.walk_children() if isinstance(item, discord.ui.Button)]) == 16
    assert len(manager.match_days()) == 1
    card, = view.children
    for i in range(1, len(card.children) - 1, 2):
        assert isinstance(card.children[i], discord.ui.TextDisplay)
        assert isinstance(card.children[i + 1], discord.ui.ActionRow)
        assert card.children[i].content.startswith("\u200b\n\n") is (i > 1)
    assert view.total_children_count <= 40


def test_pick_changes_remain_private_and_do_not_edit_the_daily_post(context):
    manager, store, channel, _, message = context
    async def scenario():
        await manager.tick()
        match = store.data.matches[0]
        assert not await manager.vote(match.id, 7, "C9", "Lua")
        assert await manager.vote(match.id, 7, "KC", "Lua")
        await manager.tick()
        channel.send.assert_awaited_once()
        message.edit.assert_not_awaited()
        assert "Lua" not in str(channel.send.call_args.kwargs["view"].to_components())
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
        assert embed.description.startswith(f"Your prediction for **C9** vs **KC** on {date}:\n## C9")
        assert "-# Use `/changevote` to change your prediction until 5 minutes" in embed.description
        assert "Use `/currentvotes`" in embed.description
        assert "Use `/leaderboard`" in embed.description
        assert interaction.followup.send.call_args.kwargs["view"].children[0].accent_colour.value == 0x5865F2
        assert not interaction.followup.send.call_args.args
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        await buttons[1].callback(interaction)
        embed = rendered_message(interaction.followup.send.call_args)
        assert embed.description.startswith(f"Your prediction for **C9** vs **KC** on {date}:\n## KC")
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        assert interaction.followup.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        channel.send.assert_awaited_once()
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
        assert "on Thu, 15 Oct 2026:" in text
        assert all(city not in text for city in ("London", "Berlin", "Tokyo"))
        assert "19:00" not in text and "20:00" not in text and "3:00" not in text
        user_settings.set_user_timezone(3, "Asia/Seoul")
        await manager.tick()
        updated = str(message.edit.call_args.kwargs["view"].to_components())
        assert "Seoul" in updated and "Tokyo" not in updated
        channel.send.assert_awaited_once()
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
        assert channel.send.await_count == 3
        assert get_leaderboard(123, store.data.tournament.id) == []
        assert get_delivery_record(123, store.data.matches[0].id) is None
        report, preview = interaction.followup.send.call_args_list
        assert "real Worlds leaderboard was not changed" in rendered_message(report).description
        card = rendered_message(preview)
        assert "TEST: @lua's Worlds Predictions" in card.title
        assert "Result: ||C9 3–0 KC · ✅ Correct||" in card.description
        assert "Result: ||FUR 3–0 MVK · ❌ Incorrect||" in card.description
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
        assert len([item for item in updated.walk_children() if isinstance(item, discord.ui.Button)]) == 16
        assert updated.total_children_count <= 40
        assert "Secret Friend" not in str(updated.to_components())
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


def test_future_board_posts_without_recap_then_gains_previous_day_mvp_when_complete(context):
    from services.polls import cast_vote
    manager, store, channel, current, message = context
    first, second = store.data.matches
    future = replace(first, id="future", start_time=first.start_time + timedelta(days=2))
    channel.send.side_effect = [SimpleNamespace(id=789), SimpleNamespace(id=790)]
    async def scenario():
        await manager.tick()
        for match in (first, second):
            await manager.vote(match.id, 7, match.team1_id, "Lua")
            await manager.vote(match.id, 8, match.team1_id, "Jerry")
        store.data = replace(store.data, matches=[first, second, future])
        await manager.tick()
        assert channel.send.await_count == 2
        assert "Previous match-day MVP" not in str(channel.send.call_args.kwargs["view"].to_components())
        current[0] = second.start_time + timedelta(hours=4)
        store.fetched_at = current[0]
        store.data = replace(store.data, matches=[replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id)
                                                if m.id != future.id else m for m in store.data.matches])
        await manager.tick()
        components = str(message.edit.call_args.kwargs["view"].to_components())
        assert "Previous match-day MVP" in components
        assert "Lua" in components and "2/2 correct" in components and "100.0% participation" in components
        assert "Jerry" in components
        assert "points" not in components
        assert message.edit.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert channel.send.await_count == 2
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
    manager, store, channel, _, message = context
    first = store.data.matches[0]
    store.data = replace(store.data, matches=[first])
    channel.send.side_effect = [SimpleNamespace(id=789), SimpleNamespace(id=790)]
    async def scenario():
        await manager.tick()
        await manager.vote(first.id, 7, first.team1_id, "Lua")
        store.data = replace(store.data, matches=[replace(first, start_time=first.start_time + timedelta(days=1))])
        await manager.tick()
        assert channel.send.await_count == 2
        old_view = message.edit.call_args.kwargs["view"]
        assert not any(isinstance(item, discord.ui.Button) for item in old_view.walk_children())
        assert len(get_polls(123, store.data.tournament.id)) == 1
        from services.polls import get_user_votes
        assert get_user_votes(123, store.data.tournament.id, 7)[0][1] == first.team1_id
    asyncio.run(scenario())
