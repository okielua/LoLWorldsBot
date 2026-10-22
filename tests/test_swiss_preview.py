import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from services import polls
from services.swiss_preview import SwissRoundPreview
from test_swiss_matchday import prepare
from test_voting import context


def test_real_round_preview_is_read_only_and_pages_all_eight_games(context):
    _, store, channel, current, matches = prepare(context)
    # A preview is available even before automatic publication opens.
    current[0] = matches[0].start_time - timedelta(hours=30)

    async def run():
        view = SwissRoundPreview(store.data, 123, 7, clock=lambda: current[0])
        assert len(view.matches) == 8
        assert view.total_children_count <= 40
        text = str(view.to_components())
        assert 'PREVIEW:' in text and 'Page 1/2' in text
        assert 'Round 1' in text and 'Bo1' in text
        assert 'Voting for every match closes together' in text
        assert len([item for item in view.walk_children() if isinstance(item, discord.ui.Button)]) == 13
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert all(item.disabled for item in buttons if item.label != 'Next Matches')
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                      edit_original_response=AsyncMock())
        await view.next_page(interaction)
        interaction.response.defer.assert_awaited_once()
        assert interaction.edit_original_response.call_args.kwargs['view'] is view
        assert 'Page 2/2' in str(view.to_components())
        assert view.total_children_count <= 40
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 9
        assert all(item.disabled for item in buttons if item.label != 'Previous Matches')
        await view.previous_page(interaction)
        assert 'Page 1/2' in str(view.to_components())
        assert polls.get_polls(123, store.data.tournament.id) == []
        assert not polls.get_daily_post_days(123, store.data.tournament.id)
        channel.send.assert_not_awaited()

    asyncio.run(run())
