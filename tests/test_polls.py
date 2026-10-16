from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from models import MatchStatus
from services import user_settings
from services.polls import cast_vote, create_poll, get_leaderboard, get_polls, initialize_polls, reconcile_polls
from services.tournament_data import load_tournament_data


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "polls.db")
    user_settings.initialize_database()
    initialize_polls()
    return load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")


def test_vote_can_change_before_start_but_counts_once(data):
    match = data.matches[0]
    create_poll(123, match)
    before = match.start_time - timedelta(minutes=10)
    cast_vote(123, match.id, 1, "C9", before)
    cast_vote(123, match.id, 1, "KC", before)
    result = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id="KC")])
    reconcile_polls(123, result, match.start_time)
    assert get_leaderboard(123, data.tournament.id) == [(1, 1, 1)]
    with pytest.raises(ValueError):
        cast_vote(123, match.id, 1, "C9", match.start_time)


def test_points_are_cumulative_idempotent_and_corrected_with_results(data):
    for match in data.matches:
        create_poll(123, match)
        cast_vote(123, match.id, 1, match.team1_id, match.start_time - timedelta(minutes=10))
        cast_vote(123, match.id, 2, match.team2_id, match.start_time - timedelta(minutes=10))
    completed = replace(data, matches=[replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id) for m in data.matches])
    for _ in range(2):
        reconcile_polls(123, completed, data.matches[-1].start_time)
    initialize_polls()
    assert get_leaderboard(123, data.tournament.id) == [(1, 2, 2), (2, 0, 2)]
    corrected = replace(completed, matches=[replace(m, winner_id=m.team2_id) for m in completed.matches])
    reconcile_polls(123, corrected, data.matches[-1].start_time)
    assert get_leaderboard(123, data.tournament.id) == [(2, 2, 2), (1, 0, 2)]
    assert get_leaderboard(999, data.tournament.id) == []


def test_vote_boundary_wrong_team_and_wrong_server_are_rejected(data):
    match = data.matches[0]
    create_poll(123, match)
    for guild_id, team, now in [(123, "GEN", match.start_time - timedelta(minutes=1)),
                                (123, "C9", match.start_time), (999, "C9", match.start_time)]:
        with pytest.raises(ValueError):
            cast_vote(guild_id, match.id, 1, team, now)
    assert get_leaderboard(123, data.tournament.id) == []


def test_stale_source_closes_votes_by_clock_without_assigning_results(data):
    match = data.matches[0]
    create_poll(123, match)
    cast_vote(123, match.id, 1, "C9", match.start_time - timedelta(minutes=10))
    reconcile_polls(123, data, match.start_time, fresh=False)
    assert get_polls(123, data.tournament.id)[0].closed
    assert get_leaderboard(123, data.tournament.id) == [(1, 0, 1)]


def test_prediction_cutoff_is_exactly_five_minutes_before_start(data):
    match = data.matches[0]
    create_poll(123, match)
    cutoff = match.start_time - timedelta(minutes=5)
    cast_vote(123, match.id, 1, "C9", cutoff - timedelta(microseconds=1), "Lua")
    with pytest.raises(ValueError, match="closed"):
        cast_vote(123, match.id, 1, "KC", cutoff)


def test_private_vote_history_is_scoped_to_user_server_and_tournament(data):
    from services.polls import get_user_votes
    match = data.matches[0]
    for guild in (123, 456):
        create_poll(guild, match)
        for user in (1, 2):
            cast_vote(guild, match.id, user, match.team1_id, match.start_time - timedelta(hours=1))
    cast_vote(123, match.id, 1, match.team2_id, match.start_time - timedelta(minutes=10))
    rows = get_user_votes(123, data.tournament.id, 1)
    assert len(rows) == 1
    assert rows[0][0].guild_id == 123
    assert rows[0][1] == match.team2_id
    assert not rows[0][0].closed
    assert get_user_votes(123, "other-tournament", 1) == []
    assert get_user_votes(123, data.tournament.id, 999) == []


def test_public_votes_require_a_closed_poll_and_successful_reminder_receipt(data):
    from services.polls import get_upcoming_match_votes
    from services.delivery_records import DeliveryRecord, initialize_delivery_records, record_delivery
    initialize_delivery_records()
    first, second = data.matches[:2]
    for guild, match in [(123, first), (456, first), (123, second)]:
        create_poll(guild, match)
        cast_vote(guild, match.id, 7, match.team1_id, match.start_time - timedelta(hours=1), "Lua")
    with pytest.raises(ValueError, match="private"):
        get_upcoming_match_votes(123, first.id)
    reconcile_polls(123, data, first.start_time - timedelta(minutes=5))
    with pytest.raises(ValueError, match="private"):
        get_upcoming_match_votes(123, first.id)
    record_delivery(DeliveryRecord(123, first.id, 456, 789, first.start_time - timedelta(minutes=5)))
    assert get_upcoming_match_votes(123, first.id) == [(7, first.team1_id, "Lua")]
    with pytest.raises(ValueError, match="private"):
        get_upcoming_match_votes(456, first.id)


def test_completed_vote_window_persists_and_does_not_reset_on_refresh_or_correction(data):
    from services.polls import get_recent_completed_match_ids
    match = data.matches[0]
    create_poll(123, match)
    completed = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id)])
    finished = match.start_time + timedelta(hours=3)
    reconcile_polls(123, completed, finished)
    assert get_recent_completed_match_ids(123, data.tournament.id, finished) == {match.id}
    initialize_polls()  # Restart/migration must preserve the window.
    corrected = replace(completed, matches=[replace(completed.matches[0], winner_id=match.team2_id)])
    reconcile_polls(123, corrected, finished + timedelta(minutes=45))
    assert get_recent_completed_match_ids(123, data.tournament.id, finished + timedelta(minutes=59, seconds=59)) == {match.id}
    assert get_recent_completed_match_ids(123, data.tournament.id, finished + timedelta(hours=1)) == set()
    assert get_recent_completed_match_ids(999, data.tournament.id, finished) == set()
    assert get_recent_completed_match_ids(123, "other", finished) == set()


def test_stale_source_does_not_start_completion_window(data):
    from services.polls import get_recent_completed_match_ids
    match = data.matches[0]
    create_poll(123, match)
    completed = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id)])
    reconcile_polls(123, completed, match.start_time, fresh=False)
    assert get_recent_completed_match_ids(123, data.tournament.id, match.start_time) == set()


def test_vote_change_preserves_creation_time_and_updates_last_valid_choice(data):
    match = data.matches[0]
    create_poll(123, match)
    created = match.start_time - timedelta(hours=2)
    changed = match.start_time - timedelta(minutes=10)
    cast_vote(123, match.id, 7, match.team1_id, created, "Lua")
    cast_vote(123, match.id, 7, match.team2_id, changed, "Lua")
    initialize_polls()
    with user_settings.get_connection() as connection:
        rows = connection.execute("SELECT created_at, updated_at, team_id, vote_team1_id, vote_team2_id FROM poll_votes").fetchall()
    assert rows == [(created.isoformat(), changed.isoformat(), match.team2_id, match.team1_id, match.team2_id)]


def test_legacy_database_migration_keeps_votes_and_only_backfills_proven_opportunities(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    monkeypatch.setattr(user_settings, "DATABASE_FILE", legacy)
    user_settings.initialize_database()
    with user_settings.get_connection() as connection:
        connection.executescript("""
            CREATE TABLE match_polls (guild_id INTEGER, match_id TEXT, tournament_id TEXT,
                team1_id TEXT, team2_id TEXT, start_time TEXT, closed INTEGER DEFAULT 0, winner_id TEXT,
                PRIMARY KEY(guild_id, match_id));
            CREATE TABLE poll_votes (guild_id INTEGER, match_id TEXT, user_id INTEGER, team_id TEXT,
                updated_at TEXT, PRIMARY KEY(guild_id, match_id, user_id));
            INSERT INTO match_polls VALUES(123, 'voted', 'worlds-2026', 'C9', 'KC', '2026-10-15T18:00:00+00:00', 0, NULL);
            INSERT INTO match_polls VALUES(123, 'unknown', 'worlds-2026', 'C9', 'KC', '2026-10-15T20:00:00+00:00', 0, NULL);
            INSERT INTO poll_votes VALUES(123, 'voted', 7, 'KC', '2026-10-15T17:00:00+00:00');
        """)
    initialize_polls()
    initialize_polls()
    with user_settings.get_connection() as connection:
        assert connection.execute("SELECT team_id, created_at, vote_team1_id, vote_team2_id FROM poll_votes").fetchone() == (
            "KC", "2026-10-15T17:00:00+00:00", "C9", "KC")
        assert dict(connection.execute("SELECT match_id, published_at FROM match_polls")) == {
            "voted": "2026-10-15T17:00:00+00:00", "unknown": None}


def test_myvotes_includes_unvoted_published_open_matches_only(data):
    from services.polls import get_user_votes, record_poll_opportunity
    first, second = data.matches[:2]
    now = first.start_time - timedelta(hours=1)
    for match in (first, second):
        create_poll(123, match)
    record_poll_opportunity(123, first.id, first.team1_id, first.team2_id, now)
    assert get_user_votes(123, data.tournament.id, 7) == []
    rows = get_user_votes(123, data.tournament.id, 7, include_open=True, now=now)
    assert [(poll.match_id, chosen) for poll, chosen in rows] == [(first.id, None)]
    assert get_user_votes(999, data.tournament.id, 7, include_open=True, now=now) == []
    assert get_user_votes(123, data.tournament.id, 7, include_open=True,
                          now=first.start_time - timedelta(minutes=5)) == []
    cast_vote(123, first.id, 7, first.team1_id, now)
    rows = get_user_votes(123, data.tournament.id, 7, include_open=True, now=now)
    assert [(poll.match_id, chosen) for poll, chosen in rows] == [(first.id, first.team1_id)]


def test_has_match_votes_distinguishes_missing_empty_and_other_servers(data):
    from services.polls import has_match_votes
    match = data.matches[0]
    assert not has_match_votes(123, match.id)
    create_poll(123, match)
    assert not has_match_votes(123, match.id)
    cast_vote(123, match.id, 7, match.team1_id, match.start_time - timedelta(hours=1))
    assert has_match_votes(123, match.id)
    assert not has_match_votes(456, match.id)
