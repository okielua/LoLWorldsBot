"""Combine Riot's schedule, tournament timetable and rendered bracket data.

All joins use Riot match IDs, never bracket order or guessed timestamps. Stage
pages carry the full tournament match map, including Swiss and knockout rounds.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re

import aiohttp

from services.lolesports import ENDPOINT, WORLDS_2026_ID, SourceError

logger = logging.getLogger(__name__)
TIMETABLE_HASH = "e134ea3594614159596e195260451574b67d8eb9e5a75fc5f044108a64d2988c"


def flight_text(html):
    """Decode public Next.js data strings without evaluating page JavaScript."""
    chunks = []
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
        found = re.fullmatch(r"\s*self\.__next_f\.push\((.*)\)\s*;?\s*", script, re.S)
        if found:
            try:
                value = json.loads(found[1])
                if isinstance(value, list) and len(value) > 1 and value[0] == 1 and isinstance(value[1], str):
                    chunks.append(value[1])
            except ValueError:
                continue
    return "".join(chunks)


def parse_bracket(html):
    text = flight_text(html)
    decoder, matches = json.JSONDecoder(), {}
    for found in re.finditer(r'\{\s*"__typename"\s*:\s*"Match"', text):
        try:
            match, _ = decoder.raw_decode(text, found.start())
            match_id = match.get("id")
            if (not isinstance(match_id, str) or not match_id.isdecimal()
                    or not isinstance(match.get("startTime"), str)
                    or not isinstance(match.get("matchTeams"), list)):
                continue
            try:
                when = datetime.fromisoformat(match["startTime"].replace("Z", "+00:00"))
            except ValueError:
                continue  # Flight references such as "$undefined" are not dates.
            if when.tzinfo is None:
                continue
            if match_id in matches and matches[match_id] != match:
                raise SourceError("Conflicting bracket records for the same match ID.")
            matches[match_id] = match
        except (json.JSONDecodeError, TypeError):
            continue
    if not matches:
        raise SourceError("Bracket page has no structured match map.")
    return matches


def usable_match(match, best_of):
    if not isinstance(match, dict) or best_of not in {1, 3, 5}:
        return False
    entries = match.get("matchTeams", [])
    if not isinstance(entries, list) or len(entries) != 2 or match.get("state") not in {"unstarted", "inProgress", "completed"}:
        return False
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("teamId"), str)
                or not entry["teamId"].isdecimal() or not isinstance(entry.get("code"), str)
                or not entry["code"] or not isinstance(entry.get("name"), str) or not entry["name"]):
            return False
        result = entry.get("result")
        if result is not None and not isinstance(result, dict):
            return False
        score = (result or {}).get("gameWins")
        if score is not None and (type(score) is not int or not 0 <= score <= best_of // 2 + 1):
            return False
    if entries[0]["teamId"] == entries[1]["teamId"] != "0":
        return False
    if match["state"] == "completed":
        results = [entry.get("result") or {} for entry in entries]
        scores = [result.get("gameWins") for result in results]
        outcomes = [result.get("outcome") for result in results]
        if (any(entry["teamId"] == "0" or entry["code"] == "TBD" for entry in entries)
                or any(type(score) is not int or score < 0 for score in scores)
                or sorted(outcomes, key=str) != ["loss", "win"]):
            return False
        winner = outcomes.index("win")
        return scores[winner] == best_of // 2 + 1 and scores[1 - winner] < scores[winner]
    return all((entry.get("result") or {}).get("outcome") is None for entry in entries)


def merge_sources(events, bracket, times, *, bracket_fresh=True, primary_available=True, window=None):
    result = deepcopy(events)
    enriched = 0
    for event in result:
        if event.get("tournament", {}).get("id") != WORLDS_2026_ID:
            continue
        match_id = event.get("match", {}).get("id")
        incoming = bracket.get(match_id)
        original_state = event["state"]
        if (incoming and usable_match(incoming, event["match"]["strategy"]["count"])
                and original_state != "completed"):
            current = event["matchTeams"]
            named_agree = all(old["teamId"] == "0" or old["code"] == "TBD"
                              or old["teamId"] == new["teamId"]
                              for old, new in zip(current, incoming["matchTeams"], strict=True))
            source_rank = {"unstarted": 0, "inProgress": 1, "completed": 2}
            advance = (bracket_fresh and source_rank[incoming["state"]] > source_rank[original_state])
            fills_slot = any((old["teamId"] == "0" or old["code"] == "TBD")
                             and new["teamId"] != "0" and new["code"] != "TBD"
                             for old, new in zip(current, incoming["matchTeams"], strict=True))
            if named_agree and (fills_slot or advance or not primary_available):
                teams = deepcopy(incoming["matchTeams"])
                # Persisted follows use existing team codes; retain the primary
                # code for an already-known Riot team if the bracket uses an alias.
                for old, new in zip(current, teams, strict=True):
                    if old["teamId"] != "0" and old["teamId"] == new["teamId"]:
                        new["code"] = old["code"]
                if bracket_fresh and (advance or not primary_available):
                    event["state"] = event["match"]["state"] = incoming["state"]
                if event["state"] == "unstarted":
                    for team in teams:
                        team["result"] = None
                event["matchTeams"] = teams
                event["_bracket"] = deepcopy(incoming)
                enriched += 1
            if not named_agree:
                logger.warning("Schedule/bracket opponent conflict for %s; keeping schedule identity", match_id)
        # The tournament timetable is independent of the homepage schedule.
        # Only scheduled times can be changed; historical result times stay stable.
        if event["state"] == "unstarted":
            when = times.get(match_id) or (incoming.get("startTime") if bracket_fresh and incoming else None)
            if when:
                try:
                    parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if parsed.tzinfo is not None and (window is None or window[0] <= parsed < window[1]):
                    event["startTime"] = when
    if enriched:
        logger.info("Riot bracket supplemented %s scheduled/live fixtures by exact match ID", enriched)
    return result


class MultiSourceClient:
    def __init__(self, primary, *, cache_path=None):
        self.primary = primary
        self._stage_urls = []
        self._bracket = {}
        self._last_events = []
        if cache_path and Path(cache_path).exists():
            try:
                self._last_events = json.loads(Path(cache_path).read_text(encoding="utf-8"))["events"]
                self._bracket = {event["match"]["id"]: event["_bracket"]
                                 for event in self._last_events if "_bracket" in event}
            except (OSError, ValueError, KeyError, TypeError):
                self._last_events = []

    async def _text(self, session, url):
        async with session.get(url) as response:
            response.raise_for_status()
            body = await response.text()
        if len(body) > 2_000_000:
            raise SourceError("Tournament page exceeded the response limit.")
        return body

    async def fetch_bracket(self):
        base = f"https://lolesports.com/en-GB/tournament/{WORLDS_2026_ID}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                         headers={"User-Agent": "LoLWorldsBot/1.0"}) as session:
            if not self._stage_urls:
                html = await self._text(session, base + "/overview")
                paths = re.findall(rf"/en-GB/tournament/{WORLDS_2026_ID}/stage/\d+", flight_text(html))
                self._stage_urls = list(dict.fromkeys("https://lolesports.com" + path for path in paths))
                if not self._stage_urls:
                    raise SourceError("Tournament overview has no stage links.")
            last_error = None
            for url in self._stage_urls:
                try:
                    return parse_bracket(await self._text(session, url))
                except aiohttp.ClientResponseError as error:
                    if error.status in {401, 403, 429}:
                        raise
                    last_error = error
                except (SourceError, TimeoutError) as error:
                    last_error = error
            self._stage_urls = []
            raise SourceError(f"All bracket stage pages failed: {last_error}")

    async def fetch_timetable(self):
        params = {"operationName": "GetSeasonTournamentEvents",
                  "variables": json.dumps({"hl": "en-GB", "sport": ["lol"], "tournamentId": [WORLDS_2026_ID]}, separators=(",", ":")),
                  "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": TIMETABLE_HASH}}, separators=(",", ":"))}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={
            "User-Agent": "LoLWorldsBot/1.0", "Content-Type": "application/json",
            "Accept": "application/graphql-response+json,application/json;q=0.9",
            "apollographql-client-name": "Esports Web", "apollographql-client-version": self.primary.client_version,
        }) as session:
            async with session.get(ENDPOINT, params=params) as response:
                response.raise_for_status()
                raw = await response.json()
        if raw.get("errors"):
            raise SourceError("Tournament timetable returned GraphQL errors.")
        return {entry["match"]["id"]: entry["startTime"] for entry in raw["data"]["esports"]["events"]}

    async def fetch_events(self, start, end, **kwargs):
        primary, bracket, times = await asyncio.gather(
            self.primary.fetch_events(start, end, **kwargs), self.fetch_bracket(), self.fetch_timetable(),
            return_exceptions=True)
        bracket_fresh = not isinstance(bracket, Exception)
        if bracket_fresh:
            self._bracket = bracket
        else:
            logger.warning("Riot bracket unavailable; retaining confirmed identities: %s", bracket)
            bracket = self._bracket
        if isinstance(times, Exception):
            logger.warning("Tournament timetable unavailable; using schedule/bracket times: %s", times)
            times = {}
        primary_available = not isinstance(primary, Exception)
        if not primary_available:
            # A fresh complete bracket can refresh a previously validated schedule
            # skeleton. Partial data must not make stale live/voting state fresh.
            cached_ids = {entry["match"]["id"] for entry in self._last_events}
            if (not bracket_fresh or not cached_ids or not cached_ids <= bracket.keys()
                    or not all(usable_match(bracket[entry["match"]["id"]], entry["match"]["strategy"]["count"])
                               and {"unstarted": 0, "inProgress": 1, "completed": 2}[bracket[entry["match"]["id"]]["state"]]
                               >= {"unstarted": 0, "inProgress": 1, "completed": 2}[entry["state"]]
                               for entry in self._last_events)):
                raise primary
            logger.warning("Homepage schedule unavailable; using complete live Riot bracket: %s", primary)
            primary = self._last_events
        merged = merge_sources(primary, bracket, times, bracket_fresh=bracket_fresh,
                               primary_available=primary_available, window=(start, end))
        self._last_events = deepcopy(merged)
        return merged
