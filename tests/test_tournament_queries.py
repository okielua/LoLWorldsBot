from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import date
from pathlib import Path

import pytest

from models import MatchStatus
from services.tournament_data import load_tournament_data
from services.tournament_queries import (
    get_matches_for_local_date,
    get_matches_for_stage,
    get_matches_for_team,
    get_next_match,
    get_standings,
)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data" / "worlds_2025.json"

worlds = load_tournament_data(DATA_FILE)


@pytest.mark.parametrize("zone, expected", [
    ("Europe/Berlin", ["before-utc-midnight", "after-utc-midnight", "last-minute"]),
    ("Europe/London", ["after-utc-midnight", "last-minute", "next-local-day"]),
    ("Asia/Tokyo", ["previous-utc-day", "before-utc-midnight", "after-utc-midnight"]),
])
def test_local_day_query_handles_utc_midnight_and_exclusive_end(zone, expected):
    times = [
        ("previous-utc-day", datetime(2026, 10, 14, 15, tzinfo=UTC)),
        ("before-utc-midnight", datetime(2026, 10, 14, 22, tzinfo=UTC)),
        ("after-utc-midnight", datetime(2026, 10, 15, 0, tzinfo=UTC)),
        ("last-minute", datetime(2026, 10, 15, 21, 59, tzinfo=UTC)),
        ("next-local-day", datetime(2026, 10, 15, 22, tzinfo=UTC)),
    ]
    data = replace(worlds, matches=[replace(worlds.matches[0], id=key, start_time=when) for key, when in reversed(times)])
    assert [m.id for m in get_matches_for_local_date(data, date(2026, 10, 15), zone)] == expected


def test_local_day_query_includes_both_sides_of_dst_fallback():
    times = [datetime(2026, 10, 25, hour, tzinfo=UTC) for hour in (0, 1, 22, 23)]
    data = replace(worlds, matches=[replace(worlds.matches[0], id=str(i), start_time=when) for i, when in enumerate(times)])
    assert [m.id for m in get_matches_for_local_date(data, date(2026, 10, 25), "Europe/Berlin")] == ["0", "1", "2"]


def test_votes_selection_keeps_live_recent_and_only_next_scheduled_match():
    from services.tournament_queries import get_matches_for_votes
    now = datetime(2026, 10, 15, tzinfo=UTC)
    template = worlds.matches[0]
    matches = [replace(template, id=key, status=status, start_time=now + timedelta(hours=hours))
               for key, status, hours in [
                   ("recent", MatchStatus.COMPLETED, -4),
                   ("old", MatchStatus.COMPLETED, -5),
                   ("live", MatchStatus.LIVE, -1),
                   ("next", MatchStatus.SCHEDULED, 1),
                   ("later", MatchStatus.SCHEDULED, 2),
                   ("cancelled", MatchStatus.CANCELLED, 0),
                   ("postponed", MatchStatus.POSTPONED, 0)]]
    data = replace(worlds, matches=list(reversed(matches)))
    assert [m.id for m in get_matches_for_votes(data, now, {"recent", "cancelled", "postponed"})] == ["recent", "live", "next"]


def test_previous_matches_only_returns_two_latest_completed_series():
    from services.tournament_queries import get_previous_matches
    now = datetime(2026, 10, 16, tzinfo=UTC)
    template = worlds.matches[0]
    matches = [replace(template, id=f"done-{i}", status=MatchStatus.COMPLETED,
                       start_time=now - timedelta(hours=i)) for i in (3, 1, 2)]
    matches += [replace(template, id=status.value, status=status, start_time=now - timedelta(minutes=1))
                for status in MatchStatus if status != MatchStatus.COMPLETED]
    matches.append(replace(template, id="future", status=MatchStatus.COMPLETED, start_time=now + timedelta(days=1)))
    data = replace(worlds, matches=matches)
    assert [m.id for m in get_previous_matches(data, now)] == ["done-1", "done-2"]
    assert get_previous_matches(replace(data, matches=[]), now) == []


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

    # The historical fixture is completed; explicitly model a scheduled tournament.
    scheduled_worlds = replace(
        worlds,
        matches=[replace(match, status=MatchStatus.SCHEDULED) for match in worlds.matches],
    )
    match = get_next_match(
        scheduled_worlds,
        before_worlds,
    )

    assert match is not None
    assert match.id == "w25-playin-t1-ig"


@pytest.mark.parametrize("status", [s for s in MatchStatus if s != MatchStatus.SCHEDULED])
def test_next_match_ignores_non_scheduled_matches(status):
    now = datetime(2026, 10, 14, tzinfo=UTC)
    template = worlds.matches[0]
    ineligible = replace(template, id="ineligible", status=status, start_time=now + timedelta(hours=1))
    later = replace(template, id="later", status=MatchStatus.SCHEDULED, start_time=now + timedelta(hours=3))
    next_match = replace(template, id="next", status=MatchStatus.SCHEDULED, start_time=now + timedelta(hours=2))
    data = replace(worlds, matches=[later, ineligible, next_match])

    assert get_next_match(data, now) == next_match
    assert get_next_match(replace(data, matches=[ineligible]), now) is None


def test_next_match_excludes_scheduled_matches_at_or_before_now():
    now = datetime(2026, 10, 14, tzinfo=UTC)
    matches = [
        replace(worlds.matches[0], status=MatchStatus.SCHEDULED, start_time=start)
        for start in (now - timedelta(hours=1), now)
    ]
    assert get_next_match(replace(worlds, matches=matches), now) is None


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
