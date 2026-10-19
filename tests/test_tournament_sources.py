import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.lolesports import SourceError
from services.live_tournament import LiveTournamentStore, normalize_events
from services.tournament_sources import MultiSourceClient, merge_sources, parse_bracket
from test_live_tournament import seed, events


START = datetime(2026, 10, 15, tzinfo=UTC)
END = datetime(2026, 11, 15, tzinfo=UTC)
CAPTURE = json.loads((Path(__file__).parent / "fixtures/riot_bracket_matches.json").read_text(encoding="utf-8"))


@pytest.fixture
def bracket():
    return {match["id"]: deepcopy(match) for match in CAPTURE["matches"]}


@pytest.fixture
def unknown_swiss(events):
    event = deepcopy(events[2])
    event["id"] = event["match"]["id"] = "115660540726095005"
    event["blockName"] = "Swiss"
    event["startTime"] = "2026-10-23T17:00:00Z"
    event["match"]["strategy"]["count"] = 1
    return [event]


def test_structured_stage_page_parser_reads_real_match_ids_and_times(bracket):
    text = "a:" + json.dumps({"matchDataById": bracket}) + "\n"
    html = '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
    assert parse_bracket(html) == bracket
    # Flight references and partial visual components aren't usable match data.
    partial = {"__typename": "Match", "id": "123", "startTime": "$undefined", "matchTeams": []}
    html += '<script>self.__next_f.push(' + json.dumps([1, json.dumps(partial)]) + ')</script>'
    assert parse_bracket(html) == bracket
    with pytest.raises(SourceError):
        parse_bracket('<script>untrustedJavascript()</script>')


def test_swiss_draw_fills_only_exact_matching_ids_without_mutation(seed, unknown_swiss, bracket):
    before = deepcopy(unknown_swiss)
    merged = merge_sources(unknown_swiss, bracket, {})
    match, = normalize_events(merged, seed).matches
    assert (match.id, match.team1_id, match.team2_id) == ("115660540726095005", "GEN", "IG")
    assert match.start_time == datetime(2026, 10, 23, 17, tzinfo=UTC)
    assert unknown_swiss == before
    assert len(merged) == 1  # Other bracket IDs are never guessed onto placeholders.


def test_known_opponent_conflict_and_completed_results_keep_primary(unknown_swiss, bracket):
    event = unknown_swiss[0]
    event["matchTeams"] = deepcopy(bracket[event["id"]]["matchTeams"])
    event["matchTeams"][0]["teamId"] = "777"
    event["matchTeams"][0]["code"] = "OTHER"
    merged = merge_sources(unknown_swiss, bracket, {})
    assert merged[0]["matchTeams"][0]["code"] == "OTHER"
    event["state"] = event["match"]["state"] = "completed"
    assert merge_sources(unknown_swiss, bracket, {})[0]["matchTeams"] == event["matchTeams"]


def test_fresh_bracket_can_confirm_results_but_cached_identity_cannot(unknown_swiss, bracket, seed):
    fixture = bracket[unknown_swiss[0]["id"]]
    fixture["state"] = "completed"
    for entry, wins, outcome in zip(fixture["matchTeams"], [1, 0], ["win", "loss"]):
        entry["result"] = {"gameWins": wins, "outcome": outcome}
    fresh = normalize_events(merge_sources(unknown_swiss, bracket, {}), seed).matches[0]
    assert fresh.winner_id == "GEN" and fresh.team1_score == 1
    cached = normalize_events(merge_sources(unknown_swiss, bracket, {}, bracket_fresh=False), seed).matches[0]
    assert cached.status.value == "scheduled" and cached.team1_score is None


def test_bad_secondary_results_and_times_do_not_poison_primary(unknown_swiss, bracket, seed):
    fixture = bracket[unknown_swiss[0]["id"]]
    fixture["state"] = "completed"  # No completed win/loss result: reject it.
    merged = merge_sources(unknown_swiss, bracket, {fixture["id"]: "not-a-date"}, window=(START, END))
    match = normalize_events(merged, seed).matches[0]
    assert match.team1_id == "TBD" and match.status.value == "scheduled"
    merged = merge_sources(unknown_swiss, {}, {fixture["id"]: "2030-01-01T00:00:00Z"}, window=(START, END))
    assert merged[0]["startTime"] == unknown_swiss[0]["startTime"]


def test_timetable_reorders_times_by_id_not_list_position(unknown_swiss, bracket):
    event = unknown_swiss[0]
    merged = merge_sources(unknown_swiss, bracket, {event["id"]: "2026-10-23T22:00:00Z"})
    assert merged[0]["startTime"] == "2026-10-23T22:00:00Z"
    assert [team["code"] for team in merged[0]["matchTeams"]] == ["GEN", "IG"]


def client_with(primary_events, bracket):
    primary = SimpleNamespace(client_version="test", fetch_events=AsyncMock(return_value=primary_events))
    client = MultiSourceClient(primary)
    client.fetch_bracket = AsyncMock(return_value=bracket)
    client.fetch_timetable = AsyncMock(return_value={})
    return client


def test_enriched_cache_survives_restart_and_secondary_outage(seed, unknown_swiss, bracket, tmp_path):
    path = tmp_path / "cache.json"
    client = client_with(unknown_swiss, bracket)
    store = LiveTournamentStore(seed, path, client)
    assert asyncio.run(store.refresh())
    assert store.data.matches[0].team1_id == "GEN"
    restarted = MultiSourceClient(client.primary, cache_path=path)
    restarted.fetch_bracket = AsyncMock(side_effect=OSError("bracket offline"))
    restarted.fetch_timetable = AsyncMock(side_effect=OSError("timetable offline"))
    store = LiveTournamentStore(seed, path, restarted)
    assert asyncio.run(store.refresh())
    assert store.data.matches[0].team1_id == "GEN"


def test_complete_fresh_bracket_is_primary_api_failover(unknown_swiss, bracket):
    client = client_with(unknown_swiss, bracket)
    async def scenario():
        await client.fetch_events(START, END)
        client.primary.fetch_events.side_effect = OSError("schedule API offline")
        result = await client.fetch_events(START, END)
        assert result[0]["matchTeams"][0]["code"] == "GEN"
        client.fetch_bracket.return_value = {}
        with pytest.raises(OSError):
            await client.fetch_events(START, END)
    asyncio.run(scenario())


def test_auxiliary_outage_does_not_block_working_primary(unknown_swiss):
    client = client_with(unknown_swiss, {})
    client.fetch_bracket.side_effect = OSError("blocked")
    client.fetch_timetable.side_effect = OSError("blocked")
    assert asyncio.run(client.fetch_events(START, END)) == unknown_swiss


def test_knockout_participants_are_enriched_by_the_same_reader(seed, unknown_swiss, bracket):
    event = unknown_swiss[0]
    fixture = next(match for match in bracket.values() if match["startTime"].startswith("2026-11"))
    named = deepcopy(bracket["115660540726095005"]["matchTeams"])
    fixture["matchTeams"] = named
    event["id"] = event["match"]["id"] = fixture["id"]
    event["blockName"] = "Quarterfinals"
    event["startTime"] = fixture["startTime"]
    event["match"]["strategy"]["count"] = 5
    match = normalize_events(merge_sources(unknown_swiss, bracket, {}), seed).matches[0]
    assert match.stage_id == next(stage.id for stage in seed.stages if stage.name == "Quarterfinals")
    assert (match.team1_id, match.team2_id, match.best_of) == ("GEN", "IG", 5)
