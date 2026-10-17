import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from services.team_history import TeamHistoryView


def test_all_matches_load_only_after_click_and_paginate_privately():
    load = Mock(return_value=["## __Play-ins__"] + [f"Match {i}\n" + "x" * 900 for i in range(8)])
    async def scenario():
        controls = TeamHistoryView("C9", load)
        load.assert_not_called()
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
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
