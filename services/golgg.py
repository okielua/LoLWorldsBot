"""Read tournament-filtered public gol.gg team tables, with a shared cache."""

import asyncio
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urljoin

import aiohttp

BASE = "https://gol.gg/teams/"
GOLGG_FOOTNOTES = "-# • Source: gol.gg\n-# • Play-In and Main Event stats are shown separately."
_cache = {}
_locks = {}
ALIASES = {
    "C9": {"cloud9", "cloud9kia"}, "GEN": {"geng", "gengesports"},
    "FUR": {"furia", "furiaesports"}, "DK": {"dpluskia", "damwonkia"},
    "MVK": {"mvk", "mvkesports"}, "TLAW": {"teamliquid", "teamliquidalienware"},
    "TSW": {"teamsecretwhales", "secretwhales"},
    "LOS": {"losgrandes"},
}
PROFILE_IDS = json.loads((Path(__file__).resolve().parents[1] / "data/golgg_team_ids_2026.json").read_text(encoding="utf-8"))["teams"]


def normalized(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


class TeamTable(HTMLParser):
    def __init__(self, kind="teams"):
        super().__init__()
        self.base = f"https://gol.gg/{kind}/"
        self.name_column = "Player" if kind == "players" else "Name"
        self.profile_path = "player-stats/" if kind == "players" else "team-stats/"
        self.active = False
        self.cell = None
        self.cells = []
        self.headers = []
        self.rows = []
        self.link = None
        self.tournament = None
        self.in_tournament_select = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.in_tournament_select = attrs.get("id") == "cbtournament"
        if tag == "option" and self.in_tournament_select and "selected" in attrs:
            self.tournament = attrs.get("value")
        if tag == "table" and "playerslist" in attrs.get("class", "").split():
            self.active = True
        if not self.active:
            return
        if tag == "tr":
            self.cells, self.link = [], None
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag == "a" and self.cell is not None and not self.cells:
            self.link = attrs.get("href")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_tournament_select = False
        if not self.active:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.cells:
            if self.cells[0] == self.name_column:
                self.headers = self.cells
            elif self.link and len(self.cells) == len(self.headers):
                url = urljoin(self.base, self.link)
                if url.startswith(self.base + self.profile_path):
                    self.rows.append((dict(zip(self.headers, self.cells)), url))
        elif tag == "table":
            self.active = False


def parse_table(html, tournament=None, *, kind="teams"):
    parser = TeamTable(kind)
    parser.feed(html)
    if not parser.headers or "Games" not in parser.headers or "Win rate" not in parser.headers:
        raise ValueError("gol.gg team table is unavailable")
    # Unknown tournaments can return a generic page. Never expose those numbers.
    if tournament and parser.tournament != tournament:
        return []
    rows = []
    for values, url in parser.rows:
        if tournament and f"/tournament-{tournament}/" not in unquote(url):
            continue
        if (values["Games"].isdigit() and 0 < int(values["Games"]) <= 1000
                and re.fullmatch(r"\d+(?:\.\d+)?%", values["Win rate"])):
            rows.append((values, url))
    return rows


async def fetch_table(tournament, year, *, kind="teams"):
    key = (tournament, year, kind)
    async with _locks.setdefault(key, asyncio.Lock()):
        entry = _cache.get(key)
        if entry and time.monotonic() < entry[0]:
            return entry[1]
        season = "ALL" if tournament else f"S{year - 2010}"
        url = f"https://gol.gg/{kind}/list/season-{season}/split-ALL/tournament-{quote(tournament or 'ALL', safe='')}/"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                    headers={"User-Agent": "LoLWorldsBot/1.0"}) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    html = await response.text(errors="replace")
            rows = parse_table(html, tournament, kind=kind)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            _cache[key] = (time.monotonic() + 60, None)
            return None
        _cache[key] = (time.monotonic() + 600, rows)
        return rows


def find_team(rows, team):
    aliases = {normalized(team.id), normalized(team.short_name), normalized(team.name)} | ALIASES.get(team.id, set())
    return next(((values, url) for values, url in rows or [] if normalized(values["Name"]) in aliases), None)


def safe_url(url):
    return quote(url, safe=":/-%")


def signed_difference(value):
    value = value.strip()
    if re.fullmatch(r"[+-]?\d[\d,.]*", value) and not value.startswith(("+", "-")):
        return "+" + value
    return value


def format_stat_groups(values, groups):
    blocks = []
    for heading, fields in groups:
        lines = []
        for label, key in fields:
            value = values.get(key, "")
            if not re.fullmatch(r"[+\-\d:.,%∞ ]+", value):
                continue
            if key in {"GD@15", "CSD@15", "XPD@15"}:
                value = signed_difference(value)
            if key in {"DRA%", "NASH%"}:
                value = value.rstrip("%") + "%"
            lines.append(f"{label}: **{value}**")
        if lines:
            blocks.append(f"### __{heading}__\n" + "\n".join(lines))
    return "\n\n".join(blocks)


async def team_stats_text(team, year):
    from services.spoilers import aggregate_token
    names = [f"Worlds {year} Main Event", f"Worlds {year} Play-In"]
    tables = await asyncio.gather(*(fetch_table(name, year) for name in names))
    blocks = []
    for name, rows in zip(names, tables):
        found = find_team(rows, team)
        if not found:
            continue
        values, url = found
        stats = format_stat_groups(values, (
            ("General Stats", (("Games", "Games"), ("Game Win Rate", "Win rate"), ("Average Game Duration", "Game duration"))),
            ("Economy", (("Gold Difference at 15 Minutes", "GD@15"),)),
            ("Aggression", (("Kills per Game", "Kills / game"), ("Deaths per Game", "Deaths / game"))),
            ("Objectives", (("Dragon Control", "DRA%"), ("Baron Control", "NASH%"))),
        ))
        blocks.append(f"## __{name} Stats__\n\n{stats}\n\n[View This Team’s Worlds Stats on gol.gg](<{safe_url(url)}>)")
    if not blocks:
        text = "### __Worlds Stats__\n" + ("Stats temporarily unavailable." if any(table is None for table in tables) else "Not enough data")
        profile_id = PROFILE_IDS.get(team.id) if year == 2026 else None
        if profile_id:
            url = BASE + f"team-stats/{profile_id}/split-ALL/tournament-{quote(names[0], safe='')}/"
            text += f"\n[View This Team’s Worlds Stats on gol.gg](<{safe_url(url)}>)"
        else:
            text += f"\n[Browse Worlds Team Stats on gol.gg](<{BASE}list/season-ALL/split-ALL/tournament-{quote(names[0], safe='')}/>)"
        blocks.append(text)
    return aggregate_token("\n\n".join(blocks))
