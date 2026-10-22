import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from services.team_history import HistoryPages, TeamHistoryView
from services.presentation import message_payload
from discord.ui.view import ViewStore


def test_all_matches_load_only_after_click_and_paginate_privately():
    load = Mock(return_value=["## __Play-ins__"] + [f"Match {i}\n" + "x" * 900 for i in range(8)])
    async def scenario():
        controls = TeamHistoryView("C9", load)
        load.assert_not_called()
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock())
        await controls.show_all.callback(interaction)
        load.assert_called_once()
        call = interaction.response.send_message.call_args
        assert call.kwargs["ephemeral"] is True
        card = call.kwargs["view"]
        pager = card.controls
        assert len(pager.pages) > 1
        assert all(len(page) < 3700 for page in pager.pages)
        assert pager.previous.disabled
        await pager.next_page.callback(interaction)
        assert pager.page == 1
        assert not pager.previous.disabled
        load.assert_called_once()
        controls.stop()
        pager.stop()
    asyncio.run(scenario())


def test_repeated_page_clicks_keep_one_registered_card_and_acknowledge_before_edit():
    async def scenario():
        pager = HistoryPages([f'# Schedule (Page {i}/4)\n\nMatch {i}' for i in range(1, 5)])
        card = message_payload(pager.pages[0], controls=pager)['view']
        store = ViewStore(SimpleNamespace())
        dispatched = []
        store.add_task = dispatched.append
        store.add_view(card, 123)
        acknowledgements = []
        for direction, target in [(1, 1), (-1, 0), (1, 1), (1, 2), (1, 3), (-1, 2), (-1, 1), (-1, 0)]:
            async def acknowledge():
                acknowledgements.append(target)

            async def edit(**payload):
                # A slow edit must already have its component click acknowledged.
                assert acknowledgements[-1] == target
                assert payload['view'] is card
                store.add_view(payload['view'], 123)

            click = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock(side_effect=acknowledge)),
                                    edit_original_response=AsyncMock(side_effect=edit),
                                    message=SimpleNamespace(id=123), data={}, extras={'setup_verified': True})
            button = pager.next_page if direction > 0 else pager.previous
            store.dispatch_view(2, button.custom_id, click)
            assert dispatched, 'Button callback disappeared from the Discord dispatcher'
            await dispatched.pop()
            assert pager.page == target
            assert not card.is_finished()
            assert all(button.view is card for button in (pager.previous, pager.next_page))
            assert store._views[123][(2, pager.previous.custom_id)] is pager.previous
            assert store._views[123][(2, pager.next_page.custom_id)] is pager.next_page
            assert str(target + 1) + '/4' in str(card.to_components())
            click.response.defer.assert_awaited_once()
            click.edit_original_response.assert_awaited_once()
        assert pager.previous.disabled and not pager.next_page.disabled
        card.stop()
        pager.stop()

    asyncio.run(scenario())


def test_page_click_acknowledgement_does_not_wait_for_message_edit():
    async def scenario():
        pager = HistoryPages(['# Page 1', '# Page 2'])
        message_payload(pager.pages[0], controls=pager)
        editing, release = asyncio.Event(), asyncio.Event()

        async def slow_edit(**payload):
            editing.set()
            await release.wait()

        click = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock(side_effect=slow_edit))
        task = asyncio.create_task(pager.turn(click, 1))
        await editing.wait()
        click.response.defer.assert_awaited_once()
        assert not task.done()
        release.set()
        await task
        pager._message_card.stop()
        pager.stop()

    asyncio.run(scenario())
