import asyncio
from datetime import timedelta

import discord

from services import polls
from services.swiss_preview import SwissRoundPreview
from test_swiss_matchday import prepare
from test_voting import context


def test_real_round_preview_is_read_only_and_shows_all_eight_games(context):
    _, store, channel, current, matches = prepare(context)
    # A preview is available even before automatic publication opens.
    current[0] = matches[0].start_time - timedelta(hours=30)

    async def run():
        view = SwissRoundPreview(store.data, 123, 7, clock=lambda: current[0])
        assert len(view.matches) == 8
        assert view.total_children_count <= 40
        text = str(view.to_components())
        assert 'PREVIEW:' in text and 'Page 1/2' not in text
        assert 'Round 1' in text and 'Bo1' in text
        assert 'Voting for every match closes together' in text
        assert 'Swiss Pick’ems' not in text and 'First time here?' not in text
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 17  # Sixteen team choices and the private vote panel.
        assert all(item.disabled for item in buttons)
        assert sum(len(item.content) for item in view.walk_children()
                   if isinstance(item, discord.ui.TextDisplay)) <= 4000
        card, = view.children
        headings = [item for item in card.children
                    if isinstance(item, discord.ui.TextDisplay) and ' vs ' in item.content]
        assert len(headings) == 8
        for heading in headings:
            index = card.children.index(heading)
            assert heading.content.splitlines()[-1].startswith('## ')
            row = card.children[index + 1]
            assert isinstance(row, discord.ui.ActionRow)
            assert len(row.children) == 2
        assert polls.get_polls(123, store.data.tournament.id) == []
        assert not polls.get_daily_post_days(123, store.data.tournament.id)
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_eight_games_with_timezones_and_many_voters_fit_discord_limits(context, monkeypatch):
    from services.voting import DailyPredictionsView

    manager, store, _, current, matches = prepare(context)
    monkeypatch.setattr('services.voting.get_registered_timezones', lambda: [
        'America/Toronto', 'Europe/London', 'Europe/Berlin', 'Asia/Dubai', 'Asia/Seoul'])
    participants = {match.id: [(i, f'Voter {i}') for i in range(100)] for match in matches}

    async def run():
        view = DailyPredictionsView(manager, '2026-10-23', matches, {}, participants=participants)
        assert view.page_count == 1
        assert view.total_children_count <= 40
        text = '\n'.join(item.content for item in view.walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
        assert len(text) <= 4000
        assert 'Toronto:' in text and 'Seoul:' in text
        assert 'See All Voters' in text

    asyncio.run(run())
