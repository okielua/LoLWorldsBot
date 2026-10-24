from presentation_assertions import walk_items
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import discord
import pytest

from models import MatchStatus
from services import polls
from services import user_settings
from services.background_polling import BackgroundPolling
from services.pickems_reminder import PickemsReminder
from services.server_reminders import ServerReminderDelivery
from services.server_settings import set_worlds_role
from services.voting import VotingManager
from services.voting_rules import voting_deadline
from test_voting import context


def prepare(context):
    manager, store, channel, current, _ = context
    first = datetime(2026, 10, 23, 17, tzinfo=UTC)
    teams = [team.id for team in store.data.teams if team.id != 'TBD'][:16]
    matches = [replace(store.data.matches[0], id=f'swiss-bo1-{i}', stage_id='worlds-2026-swiss',
                       best_of=1, start_time=first + timedelta(hours=i),
                       team1_id=teams[i * 2], team2_id=teams[i * 2 + 1],
                       round_name='Round 1 · 0–0 record') for i in range(8)]
    store.data = replace(store.data, matches=matches)
    for match in matches:
        match.voting_closes_at = voting_deadline(match, store.data)
    current[0] = first - timedelta(hours=8)
    store.fetched_at = current[0]
    set_worlds_role(123, 88)
    user_settings.set_followed_team_ids(99, {matches[-1].team2_id})
    return manager, store, channel, current, matches


@pytest.mark.parametrize('early_live', [False, True])
def test_swiss_scheduler_delivers_once_even_when_board_edit_is_blocked(context, early_live):
    from unittest.mock import AsyncMock
    from services.delivery_records import get_delivery_record
    from services.voting_rules import reminder_receipt_key

    manager, store, channel, current, matches = prepare(context)

    async def run():
        await manager.tick()
        await manager.vote(matches[0].id, 7, matches[0].team1_id, 'Lua')
        channel.send.reset_mock()
        if early_live:
            matches[0].status = MatchStatus.LIVE
        current[0] = matches[0].start_time - timedelta(minutes=5)
        store.fetched_at = current[0]
        async def blocked_edit(*args, **kwargs):
            await asyncio.Event().wait()
        manager._sync_day = AsyncMock(side_effect=blocked_edit)
        delivery = ServerReminderDelivery(manager.client, store, 123, clock=manager.clock,
                                          load_users=lambda: [], poll_manager=manager)
        poller = BackgroundPolling(store, lambda: [], lambda data: None, AsyncMock(),
                                  clock=manager.clock, server_delivery=delivery)
        await asyncio.wait_for(poller.preview_once(), timeout=2)
        channel.send.assert_awaited_once()
        manager._sync_day.assert_not_awaited()
        assert all(p.closed for p in polls.get_polls(123, store.data.tournament.id))
        assert get_delivery_record(123, reminder_receipt_key(store.data, matches[0])) is not None
        assert [role.id for role in channel.send.call_args.kwargs['allowed_mentions'].roles] == [88]
        # A new worker and delivery object must still honor the persisted receipt.
        poller.server_delivery = ServerReminderDelivery(manager.client, store, 123,
            clock=manager.clock, load_users=lambda: [], poll_manager=manager)
        await poller.preview_once()
        current[0] = matches[0].start_time
        store.fetched_at = current[0]
        await poller.preview_once()
        # Later games on the same Swiss matchday cannot generate another reminder.
        for match in matches[1:]:
            current[0] = match.start_time - timedelta(minutes=5)
            store.fetched_at = current[0]
            await poller.preview_once()
        channel.send.assert_awaited_once()

    asyncio.run(run())


def test_round_one_publishes_at_1900_berlin_with_worlds_role_ping_once(context):
    from zoneinfo import ZoneInfo

    manager, store, channel, current, matches = prepare(context)
    opening = matches[0].start_time - timedelta(hours=24)
    assert opening.astimezone(ZoneInfo('Europe/Berlin')).isoformat() == '2026-10-22T19:00:00+02:00'

    async def run():
        current[0] = opening - timedelta(microseconds=1)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        current[0] = opening
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_awaited_once()
        payload = channel.send.call_args.kwargs
        assert [role.id for role in payload['allowed_mentions'].roles] == [88]
        assert not payload['allowed_mentions'].everyone
        assert payload['allowed_mentions'].users is False
        assert '<@99>' not in str(payload['view'].to_components())
        text = str(payload['view'].to_components())
        assert '<@&88>' in text
        assert '## __Swiss stage (Round 1)__' in text
        assert 'Page ' not in text
        assert payload['view'].total_children_count == 40
        assert len([item for item in walk_items(payload['view'])
                    if isinstance(item, discord.ui.Button)]) == 16
        await manager.vote(matches[0].id, 7, matches[0].team1_id, 'Lua')
        await manager.refresh_participation(matches[0].id)
        assert '@Lua' in str(channel.get_partial_message.return_value.edit.call_args.kwargs['view'].to_components())
        assert channel.get_partial_message.return_value.edit.call_args.kwargs['allowed_mentions'].to_dict() == {'parse': []}
        await manager.tick()
        channel.send.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('registered', [False, True])
def test_daily_vote_dispatches_exactly_once_before_and_after_registration(context, legacy, registered):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from discord.components import _component_factory
    from discord.ui.view import ViewStore
    from services.voting import ScheduleVoteButton, LegacyDailyVoteButton

    manager, store, channel, _, matches = prepare(context)

    async def run():
        await manager.tick()
        original = channel.send.call_args.kwargs['view']
        button = next(item for item in walk_items(original) if isinstance(item, discord.ui.Button))
        if legacy:
            button.custom_id = f'worldsvote:123:{matches[0].id}:1'
        restarted = VotingManager(manager.client, store, 123, clock=manager.clock)
        manager.client.voting_manager = restarted
        dispatcher = ViewStore(SimpleNamespace())
        dispatcher.add_dynamic_items(ScheduleVoteButton, LegacyDailyVoteButton)
        if registered:
            dispatcher.add_view(original, 789)
        tasks = []
        dispatcher.add_task = tasks.append
        click = SimpleNamespace(
            guild_id=123, client=manager.client, extras={'setup_verified': True},
            user=SimpleNamespace(id=7, display_name='Lua'),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(id=789, flags=SimpleNamespace(components_v2=True),
                                    components=[_component_factory(c) for c in original.to_components()]),
            data={'component_type': 2, 'custom_id': button.custom_id})
        dispatcher.dispatch_view(2, button.custom_id, click)
        assert len(tasks) == 1, 'Each click must dispatch exactly once'
        await tasks.pop()
        click.response.defer.assert_awaited_once()
        click.followup.send.assert_awaited_once()
        assert polls.get_user_votes(123, store.data.tournament.id, 7)[0][1] == matches[0].team1_id

    asyncio.run(run())


def test_eight_bo1_day_has_two_pregame_posts_and_one_complete_round_result(context):
    manager, store, channel, current, matches = prepare(context)

    async def run():
        nonlocal manager
        await manager.tick()
        channel.send.assert_awaited_once()
        text = str(channel.send.call_args.kwargs['view'].to_components())
        assert '**All votes lock <t:' in text and '(5 minutes before game 1)**' in text
        assert 'Swiss Pick’ems also lock' not in text
        assert 'First time here?' not in text
        assert 'Page 1/2' not in text
        assert len(polls.get_published_match_ids(123, store.data.tournament.id)) == 8
        for match in matches:
            await manager.vote(match.id, 7, match.team1_id, 'Lua')
            await manager.announce_vote(match.id, 'Lua')
        # Participation edits the board; no extra announcement messages.
        assert channel.send.await_count == 1
        pickems = PickemsReminder(manager.client, store, 123, clock=lambda: current[0])
        assert not await pickems.tick()

        cutoff = matches[0].start_time - timedelta(minutes=5)
        current[0] = cutoff - timedelta(microseconds=1)
        store.fetched_at = current[0]
        await manager.vote(matches[-1].id, 7, matches[-1].team2_id, 'Lua')
        current[0] = cutoff
        store.fetched_at = current[0]
        # Database rejects an old button even before a reconciliation tick.
        with pytest.raises(ValueError, match='closed'):
            polls.cast_vote(123, matches[-1].id, 7, matches[-1].team1_id, current[0])
        for match in matches:
            with pytest.raises(ValueError, match='closed'):
                await manager.vote(match.id, 7, match.team1_id, 'Lua')

        delivery = ServerReminderDelivery(manager.client, store, 123, clock=lambda: current[0], poll_manager=manager)
        for match in matches:
            assert await delivery.deliver(match.id) is (match.id == matches[0].id)
        assert channel.send.await_count == 2
        reminder = str(channel.send.call_args.kwargs['view'].to_components())
        assert 'all 8 matches' in reminder
        assert [user.id for user in channel.send.call_args.kwargs['allowed_mentions'].users] == [99]
        assert all(p.closed for p in polls.get_polls(123, store.data.tournament.id))

        # Restart the delivery and voting managers: no duplicate daily/reminder.
        manager = VotingManager(delivery.client, store, 123, clock=lambda: current[0])
        delivery = ServerReminderDelivery(manager.client, store, 123, clock=lambda: current[0], poll_manager=manager)
        assert not await delivery.deliver(matches[0].id)
        await manager.tick()
        assert channel.send.await_count == 2
        for index, match in enumerate(matches):
            current[0] = match.start_time + timedelta(minutes=35)
            store.fetched_at = current[0]
            match.status = MatchStatus.COMPLETED
            match.winner_id = match.team1_id
            match.team1_score, match.team2_score = 1, 0
            await manager.tick()
            assert channel.send.await_count == (3 if index == 7 else 2)
            await manager.tick()
            assert channel.send.await_count == (3 if index == 7 else 2)
        result = str(channel.send.call_args.kwargs['view'].to_components())
        assert 'Swiss stage (Round 1)' in result and result.count('Final score: 1–0') == 8
        assert '✅ @Lua' in result and '❌ @Lua' in result
        assert channel.send.call_args.kwargs['allowed_mentions'].roles[0].id == 88
        from services.series_results import latest_result
        receipt = latest_result(123, store.data.tournament.id, match_id=matches[0].id)
        assert receipt and 'Swiss stage (Round 1)' in receipt[2][0]
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        assert channel.send.await_count == 3

    asyncio.run(run())


def test_batch_deadline_survives_stale_data_and_restart(context):
    manager, store, _, current, matches = prepare(context)
    for match in matches:
        polls.create_poll(123, match, store.data)
    current[0] = matches[0].start_time - timedelta(minutes=5)
    polls.reconcile_polls(123, store.data, current[0], fresh=False)
    assert all(p.closed for p in polls.get_polls(123, store.data.tournament.id))
    assert all(p.start_time == m.start_time for p, m in zip(polls.get_polls(123, store.data.tournament.id), matches))
    for match in matches:
        with pytest.raises(ValueError):
            polls.cast_vote(123, match.id, 7, match.team1_id, current[0])


def test_combined_results_wait_for_eighth_match_and_ignore_old_single_receipt(context):
    from services.series_results import initialize, latest_result
    manager, store, channel, current, matches = prepare(context)
    async def run():
        await manager.tick()
        initialize()
        with user_settings.get_connection() as db:
            db.execute('INSERT INTO series_result_posts VALUES (?, ?, ?, NULL, NULL, NULL, NULL)',
                       (123, store.data.tournament.id, matches[1].id))
        channel.send.reset_mock()
        current[0] = matches[-1].start_time + timedelta(hours=1)
        store.fetched_at = current[0]
        for match in matches:
            match.status = MatchStatus.COMPLETED
            match.winner_id = match.team1_id
            match.team1_score, match.team2_score = 1, 0
        matches[-1].team1_score = None  # An incomplete result cannot release the round.
        await manager.tick()
        channel.send.assert_not_awaited()
        matches[-1].team1_score = 1
        removed = store.data.matches.pop()
        await manager.tick()
        channel.send.assert_not_awaited()  # Seven source fixtures are not a complete Round 1.
        store.data.matches.append(removed)
        await manager.tick()
        channel.send.assert_awaited_once()
        assert latest_result(123, store.data.tournament.id, match_id=matches[1].id)
    asyncio.run(run())


@pytest.mark.parametrize('voter_count', [5, 100])
def test_combined_round_keeps_all_eight_results_in_first_page(context, monkeypatch, voter_count):
    from services import spoilers
    from services.series_results import swiss_result_pages
    from services.presentation import message_payload
    _, store, _, current, matches = prepare(context)
    for match in matches:
        match.status = MatchStatus.COMPLETED
        match.winner_id = match.team1_id
        match.team1_score, match.team2_score = 1, 0
    policy = spoilers.SpoilerPolicy(store.data, clock=lambda: current[0])
    monkeypatch.setattr(spoilers, '_current', policy)
    votes = {m.id: [(i, m.team1_id if i % 2 else m.team2_id,
                    f'Player {i}' + ('x' * 55 if voter_count > 5 else '')) for i in range(voter_count)] for m in matches}
    pages = swiss_result_pages(matches, votes)
    assert pages[0].count('Final score: 1–0') == 8
    assert len(pages) == 1 if voter_count == 5 else len(pages) > 1
    for page in pages:
        rendered = spoilers.render_text(page)
        assert len(rendered) < 4000
        assert '||' not in rendered
        payload = message_payload(page)
        assert payload['view'].total_children_count <= 40


def test_round_two_posts_first_four_then_all_eight_exactly_once_across_restarts(context):
    from services.series_results import SeriesResults, latest_result
    manager, store, channel, current, matches = prepare(context)
    for match in matches:
        match.round_name = 'Round 2 · 1–0 record'
        match.start_time += timedelta(days=1)
        match.voting_closes_at = voting_deadline(match, store.data)
    current[0] = matches[0].start_time - timedelta(hours=8)
    store.fetched_at = current[0]
    def finish(index):
        match = matches[index]
        match.status = MatchStatus.COMPLETED
        match.winner_id = match.team1_id
        match.team1_score, match.team2_score = 1, 0
        current[0] = max(current[0], match.start_time + timedelta(minutes=40))
        store.fetched_at = current[0]
    async def run():
        await manager.tick()
        for match in matches:
            await manager.vote(match.id, 7, match.team1_id, 'Lua')
        channel.send.reset_mock()
        for index in (0, 1, 2, 4):
            finish(index)
            await manager.tick()
            channel.send.assert_not_awaited()  # Must be the first four, not any four.
        finish(3)
        await manager.tick()
        channel.send.assert_awaited_once()
        text = str(channel.send.call_args.kwargs['view'].to_components())
        assert 'Intermediate results · First 4 of 8 matches' in text
        assert text.count('Final score: 1–0') == 4 and text.count('✅ @Lua') == 4
        assert channel.send.call_args.kwargs['allowed_mentions'].roles[0].id == 88
        manager.series_results = SeriesResults(manager)
        await manager.tick()
        channel.send.assert_awaited_once()
        for index in (5, 6):
            finish(index)
            await manager.tick()
            channel.send.assert_awaited_once()
        finish(7)
        await manager.tick()
        assert channel.send.await_count == 2
        final = str(channel.send.call_args.kwargs['view'].to_components())
        assert final.count('Final score: 1–0') == 8 and final.count('✅ @Lua') == 8
        assert 'Intermediate results' not in final
        receipt = latest_result(123, store.data.tournament.id, match_id=matches[0].id)
        assert receipt and receipt[2][0].count('Final score: 1–0') == 8
        manager.series_results = SeriesResults(manager)
        await manager.tick()
        assert channel.send.await_count == 2
    asyncio.run(run())


def test_round_three_publishes_all_eight_at_berlin_noon_after_clock_change(context):
    from zoneinfo import ZoneInfo
    from services.voting_rules import round_three_publication_time, voting_board_day
    manager, store, channel, current, matches = prepare(context)
    first = datetime(2026, 10, 25, 17, tzinfo=UTC)
    for index, match in enumerate(matches):
        match.round_name = 'Round 3 · 1–1 record'
        match.start_time = first + timedelta(hours=[0, 1, 2, 5, 24, 25, 26, 29][index])
        match.best_of = 1 if index in {0, 1, 4, 5} else 3
    for match in matches:
        match.voting_closes_at = voting_deadline(match, store.data)
    noon = round_three_publication_time(store.data)
    assert noon.isoformat() == '2026-10-25T12:00:00+01:00'
    assert noon.astimezone(UTC).hour == 11
    assert voting_board_day(store.data, matches[-1]) == '2026-10-25'
    assert len(manager.match_days()) == 1
    async def run():
        current[0] = noon - timedelta(microseconds=1)
        store.fetched_at = current[0]
        await manager.tick()
        channel.send.assert_not_awaited()
        current[0] = noon
        store.fetched_at = current[0]
        last_team = matches[-1].team2_id
        matches[-1].team2_id = 'TBD'
        await manager.tick()
        channel.send.assert_not_awaited()  # No partial or TBD announcement at noon.
        matches[-1].team2_id = last_team
        await manager.tick()
        channel.send.assert_awaited_once()
        payload = channel.send.call_args.kwargs
        text = str(payload['view'].to_components())
        assert 'Vote on all 8 Round 3 matchups' in text
        assert 'Bo1 votes close together' in text and 'Bo3 votes close' in text
        assert len([item for item in walk_items(payload['view']) if isinstance(item, discord.ui.Button)]) == 16
        assert [role.id for role in payload['allowed_mentions'].roles] == [88]
        assert payload['allowed_mentions'].users is False
        assert len(polls.get_published_match_ids(123, store.data.tournament.id)) == 8
        # A Monday pick refreshes this same Sunday round board.
        await manager.vote(matches[-1].id, 999, last_team, 'Newcomer')
        await manager.refresh_participation(matches[-1].id)
        assert 'Newcomer' in str(channel.get_partial_message.return_value.edit.call_args.kwargs['view'].to_components())
        restarted = VotingManager(manager.client, store, 123, clock=lambda: current[0])
        await restarted.tick()
        channel.send.assert_awaited_once()
        assert all(p.match_id in {m.id for m in matches} for p in polls.get_polls(123, store.data.tournament.id))
    asyncio.run(run())


def test_swiss_bo3_keeps_individual_cutoffs_and_other_days_are_independent(context):
    _, store, _, _, matches = prepare(context)
    next_day = replace(matches[0], id='next-day', start_time=matches[0].start_time + timedelta(days=1))
    bo3 = replace(matches[1], id='bo3', best_of=3)
    store.data.matches.extend([next_day, bo3])
    assert voting_deadline(next_day, store.data) == next_day.start_time - timedelta(minutes=5)
    assert voting_deadline(bo3, store.data) == bo3.start_time - timedelta(minutes=5)


def test_source_polling_is_fifteen_seconds_during_swiss_bo1_session(context):
    manager, store, _, current, matches = prepare(context)
    poller = BackgroundPolling(store, lambda: [], lambda _: None, manager.client.fetch_channel,
                               clock=lambda: current[0])
    assert poller.refresh_interval_seconds() == 60
    current[0] = matches[0].start_time
    assert poller.refresh_interval_seconds() == 15


@pytest.mark.parametrize('best_of', [1, 3])
@pytest.mark.parametrize('single_match', [True, False])
def test_all_swiss_votes_suppress_participation_announcements(context, best_of, single_match):
    manager, store, channel, _, matches = prepare(context)
    store.data = replace(store.data, matches=[replace(match, best_of=best_of)
                                             for match in (matches[:1] if single_match else matches)])

    async def run():
        for match in store.data.matches:
            await manager.announce_vote(match.id, 'Lua')
        channel.send.assert_not_awaited()

    asyncio.run(run())
