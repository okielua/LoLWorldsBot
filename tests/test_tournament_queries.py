from datetime import UTC, datetime
from pathlib import Path

from services.tournament_data import load_tournament_data
from services.tournament_queries import (
    get_matches_for_stage,
    get_matches_for_team,
    get_next_match,
    get_standings,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data" / "worlds_2025.json"

worlds = load_tournament_data(DATA_FILE)


def test_get_matches_for_team():
    matches = get_matches_for_team(worlds, "T1")

    assert len(matches) > 0

    for match in matches:
        assert "T1" in {
            match.team1_id,
            match.team2_id,
        }


def test_get_matches_for_stage():
    matches = get_matches_for_stage(
        worlds,
        "worlds-2025-final",
    )

    assert len(matches) == 1
    assert matches[0].team1_id == "T1"
    assert matches[0].team2_id == "KT"


def test_get_next_match():
    before_worlds = datetime(
        2025,
        10,
        1,
        tzinfo=UTC,
    )

    match = get_next_match(
        worlds,
        before_worlds,
    )

    assert match is not None
    assert match.id == "w25-playin-t1-ig"


def test_no_next_match_after_worlds():
    after_worlds = datetime(
        2025,
        12,
        1,
        tzinfo=UTC,
    )

    match = get_next_match(
        worlds,
        after_worlds,
    )

    assert match is None


def test_swiss_standings():
    standings = get_standings(
        worlds,
        "worlds-2025-swiss",
    )

    assert len(standings) > 0

    team_ids = {
        standing.team_id
        for standing in standings
    }

    assert "T1" in team_ids
    assert "GEN" in team_ids