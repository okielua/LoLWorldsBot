import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import golgg_champions as champions


def table(*, tournament=champions.TOURNAMENT, wins='1', losses='2', games='3', name='Rumble'):
    return (f"<select id='cbtournament'><option selected value='{tournament}'>Selected</option></select>"
            "<table class='playerslist'><tr><th>Champion</th><th>Picks</th><th>Wins</th><th>Losses</th></tr>"
            f"<tr><td><a href='./champion-stats/75/season-ALL/split-ALL/tournament-{tournament}/'>{name}</a></td>"
            f'<td>{games}</td><td>{wins}</td><td>{losses}</td></tr></table>')


def test_exact_tournament_counts_are_used_instead_of_rounded_display_percent():
    assert champions.parse_champion_winrates(table()) == {'rumble': (1, 3)}


@pytest.mark.parametrize('html', [table(tournament='Worlds 2026 Play-In'), table(tournament='ALL'),
    table(wins='2', losses='2'), table(wins='-1'), '<html>Access denied</html>',
    table().replace('/tournament-Worlds 2026 Main Event/', '/tournament-ALL/')])
def test_invalid_or_wrong_scope_data_is_rejected(html):
    with pytest.raises(ValueError):
        champions.parse_champion_winrates(html)


def test_reply_fetches_again_on_each_command_and_shows_two_decimals(monkeypatch):
    fetch = AsyncMock(side_effect=[{'rumble': (1, 3)}, {'rumble': (2, 4)}])
    monkeypatch.setattr(champions, 'fetch_champion_winrates', fetch)
    async def run():
        assert await champions.champion_winrate_reply('Rumble') == "## Worlds 2026\\* winrate for Rumble: __33.33%__\n\n-# \\*excluding play-ins"
        assert '__50.00%__' in await champions.champion_winrate_reply('rumble')
    asyncio.run(run())
    assert fetch.await_count == 2


def test_unplayed_champions_are_not_given_a_zero_percent_winrate(monkeypatch):
    rows = champions.parse_champion_winrates(table(games='0', wins='', losses=''))
    monkeypatch.setattr(champions, 'fetch_champion_winrates', AsyncMock(return_value=rows))
    text = asyncio.run(champions.champion_winrate_reply('Rumble'))
    assert 'no games recorded' in text and '%' not in text


def test_champion_name_difference_and_source_failure(monkeypatch):
    monkeypatch.setattr(champions, 'fetch_champion_winrates', AsyncMock(return_value={'nunu': (0, 1)}))
    assert '__0.00%__' in asyncio.run(champions.champion_winrate_reply('Nunu & Willump'))
    monkeypatch.setattr(champions, 'fetch_champion_winrates', AsyncMock(side_effect=TimeoutError))
    assert 'temporarily unavailable' in asyncio.run(champions.champion_winrate_reply('Rumble'))


def test_autocomplete_includes_entire_roster_and_normalizes_punctuation():
    assert len(champions.champion_catalog()) >= 173
    assert champions.champion_catalog('kaisa') == ["Kai'Sa"]
    assert champions.champion_catalog('nunu') == ['Nunu & Willump']
    assert asyncio.run(champions.champion_autocomplete(None, 'rum'))[0].value == 'Rumble'
    assert len(asyncio.run(champions.champion_autocomplete(None, ''))) == 25


def test_command_defers_before_fetch_and_returns_only_the_requested_sentence():
    from test_bot_commands import load_command
    expected = "## Worlds 2026\\* winrate for Rumble: __33.33%__\n\n-# \\*excluding play-ins"
    caller = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    async def reply(name):
        caller.response.defer.assert_awaited_once()
        assert name == 'Rumble'
        return expected
    import discord
    callback = load_command('winrate', None, discord=discord, champion_winrate_reply=reply)
    asyncio.run(callback(caller, 'Rumble'))
    from presentation_assertions import rendered_message
    card = rendered_message(caller.followup.send.call_args)
    assert card.title == 'Worlds 2026\\* winrate for Rumble: __33.33%__'
    assert '-# ' in card.description and '\\*excluding play-ins' in card.description
    assert caller.followup.send.call_args.kwargs['allowed_mentions'].to_dict() == {'parse': []}
