from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from models import MatchStatus
from services import user_settings
from services.polls import cast_vote, create_poll, initialize_polls, record_poll_opportunity, reconcile_polls
from services.prediction_stats import get_competition_stats, get_previous_day_mvp
from services.tournament_data import load_tournament_data


@pytest.fixture
def competition(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "competition.db")
    user_settings.initialize_database()
    initialize_polls()
    source = load_tournament_data(Path(__file__).resolve().parents[1] / "data" / "worlds_2026.json")
    def build(count, *, publish=True):
        matches = [replace(source.matches[0], id=f"m{i}", start_time=datetime(2026, 10, 15, 18, tzinfo=UTC) + timedelta(minutes=30*i)) for i in range(count)]
        for match in matches:
            create_poll(123, match)
            if publish:
                record_poll_opportunity(123, match.id, match.team1_id, match.team2_id, match.start_time - timedelta(hours=1))
        return replace(source, matches=matches)
    return build


def pick(match, user, correct=True):
    cast_vote(123, match.id, user, match.team1_id if correct else match.team2_id,
              match.start_time - timedelta(minutes=10), f"Friend {user}")


def complete(data):
    result = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id) for match in data.matches])
    reconcile_polls(123, result, result.matches[-1].start_time + timedelta(hours=3))
    return result


def test_accuracy_and_participation_use_different_denominators(competition):
    data = competition(4)
    pick(data.matches[0], 7)
    pick(data.matches[1], 7, False)
    complete(data)
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.submitted, row.eligible) == (1, 2, 4)
    assert row.accuracy == Fraction(1, 2)
    assert row.participation == Fraction(1, 2)
    assert get_competition_stats(999, data.tournament.id) == []
    assert get_competition_stats(123, "other") == []


def test_unknown_pending_cancelled_and_postponed_results_are_not_scored(competition):
    data = competition(5)
    for match in data.matches:
        pick(match, 7)
    matches = [replace(match, status=status, winner_id=match.team1_id if index == 0 else None)
               for index, (match, status) in enumerate(zip(data.matches,
               [MatchStatus.COMPLETED, MatchStatus.COMPLETED, MatchStatus.LIVE, MatchStatus.CANCELLED, MatchStatus.POSTPONED]))]
    reconcile_polls(123, replace(data, matches=matches), data.matches[-1].start_time)
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.submitted, row.eligible) == (1, 1, 1)


def test_unpublished_and_late_publications_do_not_penalize_participation(competition):
    data = competition(3, publish=False)
    for match in data.matches:
        pick(match, 7)
    first, late, never = data.matches
    record_poll_opportunity(123, first.id, first.team1_id, first.team2_id, first.start_time - timedelta(minutes=6))
    record_poll_opportunity(123, late.id, late.team1_id, late.team2_id, late.start_time - timedelta(minutes=5))
    complete(data)
    initialize_polls()  # Must not infer a new publication from these test votes.
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.submitted, row.eligible) == (1, 1, 1)
    assert row.participation == 1


def test_zero_correct_and_zero_predictions_do_not_divide_by_zero(competition):
    data = competition(2)
    assert get_competition_stats(123, data.tournament.id) == []
    pick(data.matches[0], 7, False)
    complete(data)
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.submitted, row.eligible, row.accuracy) == (0, 1, 2, 0)
    assert row.current_streak == row.longest_streak == 0


def test_total_correct_outranks_perfect_single_prediction(competition):
    data = competition(4)
    for match in data.matches:
        pick(match, 1, match != data.matches[-1])
    pick(data.matches[0], 2)
    complete(data)
    assert [row.user_id for row in get_competition_stats(123, data.tournament.id)] == [1, 2]


def test_ties_use_accuracy_then_stable_user_id(competition):
    data = competition(4)
    for match in data.matches:
        pick(match, 1, match in data.matches[:2])
    for match in data.matches[:3]:
        pick(match, 3, match in data.matches[:2])
        pick(match, 2, match in data.matches[:2])
    complete(data)
    assert [row.user_id for row in get_competition_stats(123, data.tournament.id)] == [2, 3, 1]


def test_streaks_skip_missed_and_pending_matches_and_reset_on_wrong_pick(competition):
    data = competition(9)
    for index, match in enumerate(data.matches):
        if index != 2:  # A missing prediction does not reset the streak.
            pick(match, 7, index not in {4, 5})
    completed = replace(data, matches=[replace(m, status=MatchStatus.COMPLETED, winner_id=m.team1_id)
                                      if i != 5 else replace(m, status=MatchStatus.LIVE) for i, m in enumerate(data.matches)])
    reconcile_polls(123, completed, data.matches[-1].start_time)
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.submitted, row.eligible) == (6, 7, 8)
    assert (row.current_streak, row.longest_streak) == (3, 3)
    initialize_polls()
    assert get_competition_stats(123, data.tournament.id) == [row]


def test_official_corrections_recompute_totals_and_streaks_without_double_scoring(competition):
    data = competition(3)
    for match in data.matches:
        pick(match, 7)
    completed = complete(data)
    complete(data)
    assert get_competition_stats(123, data.tournament.id)[0].correct == 3
    corrected = replace(completed, matches=[replace(m, winner_id=m.team2_id) if i == 1 else m for i, m in enumerate(completed.matches)])
    reconcile_polls(123, corrected, completed.matches[-1].start_time + timedelta(hours=4))
    row, = get_competition_stats(123, data.tournament.id)
    assert (row.correct, row.current_streak, row.longest_streak) == (2, 1, 1)


def test_changed_pairing_keeps_votes_but_does_not_score_old_matchup(competition):
    data = competition(1)
    match = data.matches[0]
    pick(match, 7)
    changed = replace(match, team2_id="FUR")
    reconcile_polls(123, replace(data, matches=[changed]), match.start_time - timedelta(minutes=8))
    record_poll_opportunity(123, changed.id, changed.team1_id, changed.team2_id, changed.start_time - timedelta(minutes=7))
    complete(replace(data, matches=[changed]))
    assert get_competition_stats(123, data.tournament.id) == []
    with user_settings.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 1


def test_previous_match_day_mvp_uses_correct_then_accuracy_and_joint_winners(competition):
    data = competition(8)
    for i, match in enumerate(data.matches):
        pick(match, 1, i < 6)
        if i < 6:
            pick(match, 2)
            pick(match, 3)
    pick(data.matches[0], 4)  # 100% accuracy alone cannot win over six correct.
    completed = complete(data)
    winners = get_previous_day_mvp(123, completed, "2026-10-17")  # No games on Oct 16.
    assert [row.user_id for row in winners] == [2, 3]
    assert all((row.correct, row.submitted, row.eligible) == (6, 6, 8) for row in winners)
    assert all(row.participation == Fraction(3, 4) for row in winners)


def test_incomplete_previous_day_never_declares_an_mvp(competition):
    data = competition(2)
    pick(data.matches[0], 7)
    first_done = replace(data, matches=[replace(data.matches[0], status=MatchStatus.COMPLETED, winner_id=data.matches[0].team1_id), data.matches[1]])
    reconcile_polls(123, first_done, data.matches[-1].start_time)
    assert get_previous_day_mvp(123, first_done, "2026-10-16") == []
    assert get_previous_day_mvp(123, first_done, "2026-10-15") == []
