"""Normalize and cache complete LoL Esports snapshots without changing user settings."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import logging
from pathlib import Path

import aiohttp

from models import Match, MatchStatus, Team, TournamentStatus
from services.lolesports import LoLEsportsClient, SourceError, WORLDS_2026_ID
from services.tournament_data import TournamentData, parse_datetime, validate_tournament_data
from services.standings import calculate_swiss_standings
from services.swiss import swiss_round_label


logger = logging.getLogger(__name__)
TBD_TEAM_ID = "TBD"
STAGE_NAMES = {
    "Play-Ins": "Play-In", "Swiss": "Swiss Stage",
    "Quarterfinals": "Quarterfinals", "Semifinals": "Semifinals", "Finals": "Final",
}
STATES = {
    "unstarted": MatchStatus.SCHEDULED,
    "inProgress": MatchStatus.LIVE,
    "completed": MatchStatus.COMPLETED,
}


def normalize_events(events: list[dict], seed: TournamentData) -> TournamentData:
    try:
        return _normalize_events(events, seed)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise SourceError(f"Invalid tournament snapshot: {error}") from error


def _normalize_events(events, seed):
    if not isinstance(events, list):
        raise SourceError("Events must be a list.")
    teams = seed.teams_by_id.copy()
    stages = {stage.name: stage for stage in seed.stages}
    matches = []
    match_ids = set()
    source_team_ids = {}
    for event in events:
        if event["tournament"]["id"] != WORLDS_2026_ID:
            continue
        if event["type"] != "match":
            raise SourceError("Unexpected non-match event.")
        match_id = event["match"]["id"]
        if not isinstance(match_id, str) or not match_id.isdecimal() or event["id"] != match_id:
            raise SourceError("Invalid or mismatched match ID.")
        if match_id in match_ids:
            raise SourceError("Duplicate match ID.")
        match_ids.add(match_id)
        state = STATES[event["state"]]  # Unknown states never become scheduled.
        if event["match"]["state"] != event["state"]:
            raise SourceError("Event and match states disagree.")
        stage = stages[STAGE_NAMES[event["blockName"]]]
        start = parse_datetime(event["startTime"]).astimezone(UTC)
        if not seed.tournament.start_date <= start.date() <= seed.tournament.end_date:
            raise SourceError("Match time falls outside the tournament dates.")
        strategy = event["match"]["strategy"]
        best_of = strategy["count"]
        if strategy["type"] != "bestOf" or type(best_of) is not int or best_of not in {1, 3, 5}:
            raise SourceError("Invalid series format.")
        entries = event["matchTeams"]
        if not isinstance(entries, list) or len(entries) != 2:
            raise SourceError("A match must have exactly two team slots.")
        ids, scores, outcomes = [], [], []
        for entry in entries:
            source_id = entry["teamId"]
            code = entry["code"]
            if not isinstance(source_id, str) or not source_id.isdecimal():
                raise SourceError("Invalid source team ID.")
            if source_id == "0" or code == "TBD":
                team_id = TBD_TEAM_ID
                teams[team_id] = Team(team_id, "To be determined", "TBD")
            else:
                if not isinstance(code, str) or not code or not isinstance(entry["name"], str) or not entry["name"]:
                    raise SourceError("Invalid team identity.")
                if code in source_team_ids and source_team_ids[code] != source_id:
                    raise SourceError("Two source teams share a team code.")
                source_team_ids[code] = source_id
                team_id = code  # Preserve existing SQLite follows and /team IDs.
                existing = teams.get(code)
                teams[code] = Team(
                    code, entry["name"], code,
                    existing.region if existing else None,
                    entry.get("image") or (existing.logo_url if existing else None),
                )
            ids.append(team_id)
            result = entry.get("result")
            score = result.get("gameWins") if result else None
            outcome = result.get("outcome") if result else None
            if score is not None and (type(score) is not int or not 0 <= score <= best_of // 2 + 1):
                raise SourceError("Invalid series score.")
            if outcome not in {None, "win", "loss"}:
                raise SourceError("Unknown result outcome.")
            scores.append(score)
            outcomes.append(outcome)
        if ids[0] == ids[1] and ids[0] != TBD_TEAM_ID:
            raise SourceError("A team cannot play itself.")
        winner = None
        if state == MatchStatus.COMPLETED:
            if TBD_TEAM_ID in ids or sorted(outcomes, key=str) != ["loss", "win"] or None in scores:
                raise SourceError("Completed match lacks a valid result.")
            winner_index = outcomes.index("win")
            if scores[winner_index] != best_of // 2 + 1 or scores[1 - winner_index] >= scores[winner_index]:
                raise SourceError("Winner and series score disagree.")
            winner = ids[winner_index]
        elif any(outcome is not None for outcome in outcomes):
            raise SourceError("Unfinished match has a final result.")
        # Retain known round labels from the seed without deriving new match IDs.
        original = next((m for m in seed.matches if
            m.start_time == start and (m.team1_id, m.team2_id) == tuple(ids)), None)
        matches.append(Match(
            id=match_id, tournament_id=seed.tournament.id, stage_id=stage.id,
            team1_id=ids[0], team2_id=ids[1], start_time=start, best_of=best_of,
            round_name=(swiss_round_label(event.get("_swiss_description")) if event["blockName"] == "Swiss"
                        else original.round_name if original else None), status=state,
            team1_score=scores[0] if state != MatchStatus.SCHEDULED else None,
            team2_score=scores[1] if state != MatchStatus.SCHEDULED else None,
            winner_id=winner,
        ))
    if not matches:
        raise SourceError("Snapshot contains no Worlds 2026 matches.")
    status = TournamentStatus.UPCOMING
    if all(m.status == MatchStatus.COMPLETED for m in matches):
        status = TournamentStatus.COMPLETED
    elif any(m.status in {MatchStatus.LIVE, MatchStatus.COMPLETED} for m in matches):
        status = TournamentStatus.ONGOING
    data = replace(
        seed, tournament=replace(seed.tournament, status=status), teams=list(teams.values()),
        matches=sorted(matches, key=lambda m: m.start_time),
    )
    validate_tournament_data(data)
    data.standings = calculate_swiss_standings(data)
    return data


class LiveTournamentStore:
    def __init__(self, seed: TournamentData, cache_path: Path, client=None):
        self.seed = seed
        self.data = seed
        self.cache_path = Path(cache_path)
        self.client = client or LoLEsportsClient()
        self.fetched_at = None
        self.source = "seed"
        self.last_error = None
        self.retry_not_before = None
        self._refresh_lock = asyncio.Lock()
        if self.cache_path.exists():
            try:
                snapshot = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if snapshot["schema_version"] != 1:
                    raise SourceError("Unsupported cache version.")
                fetched_at = parse_datetime(snapshot["fetched_at"]).astimezone(UTC)
                if fetched_at > datetime.now(UTC) + timedelta(minutes=5):
                    raise SourceError("Cache timestamp is in the future.")
                self.data = normalize_events(snapshot["events"], seed)
                self.fetched_at = fetched_at
                self.source = "cache"
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.last_error = str(error)
                logger.warning("Ignoring invalid tournament cache: %s", error)

    async def refresh(self) -> bool:
        async with self._refresh_lock:
            try:
                start = datetime.combine(self.seed.tournament.start_date, datetime.min.time(), UTC)
                end = datetime.combine(self.seed.tournament.end_date + timedelta(days=1), datetime.min.time(), UTC)
                events = await asyncio.wait_for(self.client.fetch_events(start, end), timeout=45)
                data = normalize_events(events, self.seed)
                if self.source != "seed" and len(data.matches) < len(self.data.matches) / 2:
                    raise SourceError(
                        f"Schedule shrank from {len(self.data.matches)} to {len(data.matches)} matches; retaining previous snapshot."
                    )
                fetched_at = datetime.now(UTC)
                snapshot = {"schema_version": 1, "fetched_at": fetched_at.isoformat(), "events": events}
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
                temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.cache_path)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
                self.last_error = str(error)
                self.retry_not_before = None
                if isinstance(error, aiohttp.ClientResponseError):
                    if error.status in {401, 403}:
                        self.last_error = f"Source HTTP {error.status}: check LOLESPORTS_CLIENT_VERSION and LOLESPORTS_QUERY_HASH."
                    elif error.status == 429:
                        retry_after = (error.headers or {}).get("Retry-After", "60")
                        try:
                            self.retry_not_before = datetime.now(UTC) + timedelta(seconds=max(0, float(retry_after)))
                        except (ValueError, OverflowError):
                            try:
                                self.retry_not_before = parsedate_to_datetime(retry_after).astimezone(UTC)
                            except (ValueError, TypeError, OverflowError):
                                self.retry_not_before = datetime.now(UTC) + timedelta(seconds=60)
                logger.warning("Live refresh failed; retaining %s data: %s", self.source, self.last_error)
                return False
            self.data = data
            self.fetched_at = fetched_at
            self.source = "live"
            self.last_error = None
            self.retry_not_before = None
            logger.info("Refreshed %s matches from LoL Esports", len(data.matches))
            return True
