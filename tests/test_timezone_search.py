import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services import timezone_search
from services.presentation import message_payload
from services.timezone_search import CityMatchesView, CityResultSelect, CitySearchButton, city_matches


@pytest.mark.parametrize('query,zone', [
    ('dub', 'Asia/Dubai'), ('DUBAI', 'Asia/Dubai'), ('lond', 'Europe/London'),
    ('São Paulo', 'America/Sao_Paulo'), ('sao paulo', 'America/Sao_Paulo'),
    ('osaka', 'Asia/Tokyo'), ('berlin', 'Europe/Berlin'), ('Asia/Dubai', 'Asia/Dubai'),
])
def test_city_search_supports_partial_names_aliases_accents_and_zone_ids(query, zone):
    assert any(found == zone for _, found in city_matches(query))


def test_city_results_are_bounded_and_unknown_cities_are_not_guessed():
    assert city_matches('') == []
    assert city_matches('d') == []
    assert city_matches('xyznonexistentcity') == []
    assert 0 < len(city_matches('america')) <= 25
    assert len({zone for _, zone in city_matches('america')}) == len(city_matches('america'))


def test_search_modal_and_selection_edit_the_same_card_and_preserve_expiry(monkeypatch):
    save = Mock()
    monkeypatch.setattr(timezone_search, 'set_user_timezone', save)
    setup = Mock(side_effect=lambda interaction: message_payload('## Setup\n\nAsia/Dubai'))

    async def run():
        button = CitySearchButton(7, setup)
        controls = CityMatchesView(7, setup, [])
        original = message_payload('## Setup', controls=controls)['view']
        original.expire_with(SimpleNamespace(extras={}, delete_original_response=AsyncMock()))
        click = SimpleNamespace(user=SimpleNamespace(id=7), extras={'expiring_message_card': original},
                                response=SimpleNamespace(send_modal=AsyncMock()))
        await button.callback(click)
        modal = click.response.send_modal.call_args.args[0]
        modal.city._value = 'dub'
        submit = SimpleNamespace(user=SimpleNamespace(id=7), extras={},
                                 response=SimpleNamespace(edit_message=AsyncMock()))
        await modal.on_submit(submit)
        save.assert_not_called()
        assert original.is_finished()
        assert original.expiry_interaction is None
        result_card = submit.response.edit_message.call_args.kwargs['view']
        result = next(item for item in result_card.walk_children() if isinstance(item, CityResultSelect))
        dubai = next(option for option in result.options if option.value == 'Asia/Dubai')
        assert dubai.label == 'Dubai · UTC+04:00'
        assert dubai.description == 'Asia/Dubai'
        result._values = ['Asia/Dubai']
        choose = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(defer=AsyncMock()),
                                 edit_original_response=AsyncMock())
        await result.callback(choose)
        save.assert_called_once_with(7, 'Asia/Dubai')
        choose.response.defer.assert_awaited_once()
        choose.edit_original_response.assert_awaited_once()
        setup.assert_called_once_with(choose)
        result_card.stop()
        controls.stop()

    asyncio.run(run())


def test_wrong_user_cannot_open_search_or_save_another_users_timezone(monkeypatch):
    save = Mock()
    monkeypatch.setattr(timezone_search, 'set_user_timezone', save)

    async def run():
        wrong = SimpleNamespace(user=SimpleNamespace(id=8),
                                response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
        await CitySearchButton(7, Mock()).callback(wrong)
        wrong.response.send_modal.assert_not_awaited()
        assert wrong.response.send_message.call_args.kwargs['ephemeral']
        result = CityResultSelect(7, Mock(), [('Dubai', 'Asia/Dubai')])
        result._values = ['Asia/Dubai']
        await result.callback(wrong)
        save.assert_not_called()

    asyncio.run(run())


def test_no_results_offers_retry_and_back_without_changing_settings(monkeypatch):
    save = Mock()
    monkeypatch.setattr(timezone_search, 'set_user_timezone', save)

    async def run():
        from services.timezone_search import CitySearchModal
        setup = Mock(return_value=message_payload('## Setup'))
        modal = CitySearchModal(7, setup)
        modal.city._value = 'xyznonexistentcity'
        submit = SimpleNamespace(user=SimpleNamespace(id=7), extras={},
                                 response=SimpleNamespace(edit_message=AsyncMock()))
        await modal.on_submit(submit)
        card = submit.response.edit_message.call_args.kwargs['view']
        assert 'No Cities Found' in str(card.to_components())
        assert not any(isinstance(item, CityResultSelect) for item in card.walk_children())
        assert any(isinstance(item, CitySearchButton) for item in card.walk_children())
        await card.controls.back.callback(submit)
        setup.assert_called_once_with(submit)
        save.assert_not_called()
        card.stop()

    asyncio.run(run())
