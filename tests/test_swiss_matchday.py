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


def test_eight_bo1_day_has_two_pregame_posts_and_eight_immediate_result_posts(context):
    manager, store, channel, current, matches = prepare(context)

    async def run():
        nonlocal manager
        await manager.tick()
        channel.send.assert_awaited_once()
        text = str(channel.send.call_args.kwargs['view'].to_components())
        assert 'Voting for every match closes together' in text
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
            assert channel.send.await_count == index + 3
            result = str(channel.send.call_args.kwargs['view'].to_components())
            assert 'Series Voting Results' in result and '1–0' in result
            assert channel.send.call_args.kwargs['allowed_mentions'].roles[0].id == 88
            await manager.tick()
            assert channel.send.await_count == index + 3
        assert channel.send.await_count == 10

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
