"""Worlds-only player statistics and a fast alphabetical autocomplete catalog."""

import asyncio
import json
from pathlib import Path
import re
from urllib.parse import quote

import discord

from services.golgg import GOLGG_FOOTNOTES, fetch_table, safe_url, format_stat_groups

_seed = json.loads((Path(__file__).resolve().parents[1] / "data/golgg_players_2026.json").read_text(encoding="utf-8"))
_catalogs = {_seed["year"]: {item["id"]: item["name"] for item in _seed["players"]}}


def player_catalog(year, query=""):
    query = query.strip().casefold()
    return sorted(((player_id, name) for player_id, name in _catalogs.get(year, {}).items()
                   if query in name.casefold()), key=lambda item: (item[1].casefold(), item[0]))


async def refresh_players(year):
    from services.spoilers import current_policy
    names = [f"Worlds {year} Main Event", f"Worlds {year} Play-In"]
    tables = await asyncio.gather(*(fetch_table(name, year, kind="players") for name in names))
    catalog = _catalogs.setdefault(year, {})
    for rows in tables:
        for values, url in rows or []:
            player_id = re.search(r"/player-stats/(\d+)/", url)
            policy = current_policy()
            safe_catalog_update = policy is None or not any(policy.protected(match) for match in policy.data.matches
                                                            if match.status.value in {"live", "completed"})
            if player_id and (player_id.group(1) in catalog or safe_catalog_update):
                catalog[player_id.group(1)] = values["Player"]
    return list(zip(names, tables))


async def refresh_players_loop(year):
    while True:
        await refresh_players(year)
        await asyncio.sleep(600)


async def player_reply(query, year):
    from services.spoilers import aggregate_token
    if not query or not query.strip():
        return "Choose a player using the required name option."
    tables = await refresh_players(year)
    catalog = player_catalog(year)
    selected = next(((player_id, name) for player_id, name in catalog
                     if query == player_id or query.strip().casefold() == name.casefold()), None)
    if selected is None:
        return "Player not found in the Worlds player list. Start typing the name and choose an autocomplete suggestion."
    player_id, name = selected
    blocks = [f"## {discord.utils.escape_markdown(name)} · Worlds {year} Stats"]
    found = False
    for tournament, rows in tables:
        row = next(((values, url) for values, url in rows or []
                    if url.split("/player-stats/", 1)[-1].split("/", 1)[0] == player_id), None)
        if row is None:
            continue
        found = True
        values, url = row
        stats = format_stat_groups(values, (
            ("General Stats", (("Games", "Games"), ("Game Win Rate", "Win rate"), ("KDA", "KDA"),
             ("Average Kills", "Avg kills"), ("Average Deaths", "Avg deaths"), ("Average Assists", "Avg assists"),
             ("CS per Minute", "CSM"))),
            ("Early Game", (("Gold Difference at 15 Minutes", "GD@15"), ("CS Difference at 15 Minutes", "CSD@15"),
             ("XP Difference at 15 Minutes", "XPD@15"))),
            ("Aggression", (("Damage per Minute", "DPM"), ("Kill Participation", "KP%"))),
        ))
        stage_name = tournament.removeprefix(f"Worlds {year} ")
        blocks.append(f"## __{stage_name}__\n\n{stats}\n\n[View {discord.utils.escape_markdown(name)}’s Stats on gol.gg](<{safe_url(url)}>)")
    if not found:
        message = "Stats temporarily unavailable." if any(rows is None for _, rows in tables) else "Not enough data"
        url = (f"https://gol.gg/players/player-stats/{player_id}/season-ALL/split-ALL/"
               f"tournament-{quote(f'Worlds {year} Main Event', safe='')}/")
        blocks.append(f"{message}\n[View This Player’s Worlds Stats on gol.gg](<{url}>)")
    return blocks[0] + "\n\n" + aggregate_token("\n\n".join(blocks[1:])) + "\n\n" + GOLGG_FOOTNOTES
