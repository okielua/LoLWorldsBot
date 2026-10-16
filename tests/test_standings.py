from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json

from models import MatchStatus
from services.live_tournament import normalize_events
from services.standings import calculate_swiss_standings
from services.tournament_data import load_tournament_data


ROOT = Path(__file__).resolve().parents[1]


def test_live_snapshot_derives_swiss_series_records_and_updates_on_correction():
    seed = load_tournament_data(ROOT / "data" / "worlds_2026.json")
    events = json.loads((ROOT / "tests" / "fixtures" / "lolesports_events.json").read_text())
    event = events[0]
    event["blockName"] = "Swiss"
    event["state"] = event["match"]["state"] = "completed"
    event["match"]["strategy"]["count"] = 3
    event["matchTeams"][0]["result"].update(gameWins=2, outcome="win")
    event["matchTeams"][1]["result"].update(gameWins=1, outcome="loss")
    data = normalize_events(events, seed)
    table = {row.team_id: row for row in data.standings}
    assert (table["C9"].wins, table["C9"].losses) == (1, 0)
    assert (table["KC"].wins, table["KC"].losses) == (0, 1)
    assert all(row.rank is None for row in table.values())
    assert set(table) == {"C9", "KC"}
    # Recalculation is idempotent and corrected source outcomes replace old results.
    assert normalize_events(events, seed).standings == data.standings
    corrected = deepcopy(events)
    corrected[0]["matchTeams"][0]["result"].update(gameWins=0, outcome="loss")
    corrected[0]["matchTeams"][1]["result"].update(gameWins=2, outcome="win")
    table = {row.team_id: row for row in normalize_events(corrected, seed).standings}
    assert table["KC"].wins == 1
    assert table["C9"].wins == 0


def test_swiss_records_exclude_tbd_and_cancelled_and_do_not_count_live_scores():
    data = load_tournament_data(ROOT / "data" / "worlds_2026.json")
    template = data.matches[0]
    swiss = next(stage for stage in data.stages if "Swiss" in stage.name)
    matches = [
        replace(template, stage_id=swiss.id, status=MatchStatus.LIVE, team1_score=1, team2_score=0),
        replace(template, id="tbd", stage_id=swiss.id, team1_id="TBD", team2_id="TBD"),
        replace(data.matches[1], stage_id=swiss.id, status=MatchStatus.CANCELLED),
    ]
    table = calculate_swiss_standings(replace(data, matches=matches))
    assert {row.team_id for row in table} == {"C9", "KC"}
    assert all(row.wins == row.losses == 0 for row in table)
