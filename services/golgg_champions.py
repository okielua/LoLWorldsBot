"""Current, tournament-scoped champion win rates and Riot champion autocomplete."""

import asyncio
import json
import logging
from pathlib import Path
import re
from urllib.parse import unquote, quote

import aiohttp
from discord import app_commands

from services.golgg import TeamTable, normalized

logger = logging.getLogger(__name__)
TOURNAMENT = 'Worlds 2026 Main Event'
URL = f"https://gol.gg/champion/list/season-ALL/split-ALL/tournament-{quote(TOURNAMENT, safe='')}/"
_names = json.loads((Path(__file__).resolve().parents[1] / 'data/champions.json').read_text(encoding='utf-8'))['names']
NAME_ALIASES = {'nunuwillump': 'nunu'}


def champion_catalog(query=''):
    query = normalized(query)
    return sorted((name for name in _names if query in normalized(name)),
                  key=lambda name: (not normalized(name).startswith(query), name.casefold()))


async def champion_autocomplete(interaction, current):
    return [app_commands.Choice(name=name, value=name) for name in champion_catalog(current)[:25]]


async def refresh_champions():
    global _names
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get('https://ddragon.leagueoflegends.com/api/versions.json') as response:
            response.raise_for_status()
            version = (await response.json())[0]
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Invalid Riot champion catalog version')
        async with session.get(f'https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json') as response:
            response.raise_for_status()
            data = await response.json()
        names = sorted({item['name'] for item in data['data'].values() if item.get('name')})
        if len(names) < len(_names):
            raise ValueError('Riot champion catalog unexpectedly shrank')
        _names = names


async def refresh_champions_loop():
    while True:
        try:
            await refresh_champions()
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            logger.warning('Could not refresh champion autocomplete; retaining existing catalog', exc_info=True)
        await asyncio.sleep(24 * 60 * 60)


def parse_champion_winrates(html):
    parser = TeamTable('champion')
    parser.name_column = 'Champion'
    parser.profile_path = 'champion-stats/'
    parser.feed(html)
    if parser.tournament != TOURNAMENT or not {'Champion', 'Picks', 'Wins', 'Losses'} <= set(parser.headers):
        raise ValueError('gol.gg did not return the Worlds 2026 Main Event champion table')
    rows = {}
    for values, url in parser.rows:
        if (not re.match(r'https://gol\.gg/champion/champion-stats/\d+/', url)
                or f'/tournament-{TOURNAMENT}/' not in unquote(url)):
            raise ValueError('Champion statistics have the wrong tournament scope')
        games = values['Picks']
        wins, losses = values['Wins'], values['Losses']
        if games == '0' and wins == losses == '':
            wins = losses = '0'
        if not all(value.isdigit() for value in (games, wins, losses)):
            raise ValueError('Invalid champion game counts')
        games, wins, losses = map(int, (games, wins, losses))
        if wins + losses != games:
            raise ValueError('Champion wins and losses do not match game count')
        rows[normalized(values['Champion'])] = (wins, games)
    if not rows:
        raise ValueError('gol.gg champion statistics are unavailable')
    return rows


async def fetch_champion_winrates():
    # Fetch on every invocation; never substitute season-wide or stale cached stats.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
            headers={'User-Agent': 'LoLWorldsBot/1.0', 'Cache-Control': 'no-cache'}) as session:
        async with session.get(URL) as response:
            response.raise_for_status()
            html = await response.text(errors='replace')
            if len(html) > 2_000_000:
                raise ValueError('gol.gg champion table exceeded the response limit')
    return parse_champion_winrates(html)


async def champion_winrate_reply(query):
    name = next((name for name in _names if normalized(name) == normalized(query)), None)
    if name is None:
        return 'Choose a champion from the autocomplete suggestions.'
    try:
        rows = await fetch_champion_winrates()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.warning('Could not fetch current Worlds champion win rates', exc_info=True)
        return 'Current Worlds 2026 Main Event win rates are temporarily unavailable on gol.gg. Please try again shortly.'
    result = rows.get(NAME_ALIASES.get(normalized(name), normalized(name)))
    if result is None:
        return f'Current Worlds 2026 Main Event data for {name} is unavailable on gol.gg.'
    wins, games = result
    if games == 0:
        return f'{name} has no games recorded so far in Worlds 2026 (excluding play-ins).'
    return f"## Worlds 2026\\* winrate for {name}:\n## __{100 * wins / games:.2f}%__ ({wins}W | {games - wins}L)\n\n-# \\*excluding play-ins"
