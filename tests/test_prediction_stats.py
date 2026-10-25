from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from models import MatchStatus
from services import user_settings
from services.polls import cast_vote, create_poll, initialize_polls, record_poll_opportunity, reconcile_polls
from services.prediction_stats import get_competition_stats, get_previous_day_mvp, get_competition_snapshot, competition_ranks
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
    advanced = get_competition_snapshot(123, data.tournament.id).advanced[7]
    assert advanced.most_backed.backed_wins == 2
    assert advanced.best_read.accuracy == Fraction(2, 3)


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


def test_movement_reconstructs_previous_scored_result_and_survives_restart(competition):
    from services.leaderboard_display import rank_movement
    data = competition(2)
    first, second = data.matches
    pick(first, 1)
    pick(first, 2)
    pick(second, 1, False)
    pick(second, 2)
    pick(second, 3)
    initial = replace(data, matches=[replace(first, status=MatchStatus.COMPLETED, winner_id=first.team1_id), second])
    reconcile_polls(123, initial, first.start_time + timedelta(minutes=10))
    before = get_competition_snapshot(123, data.tournament.id)
    completed = complete(data)
    snapshot = get_competition_snapshot(123, data.tournament.id, data=completed)
    assert snapshot.previous_rows == before.rows
    ranks, previous = competition_ranks(snapshot.rows), competition_ranks(snapshot.previous_rows)
    assert ranks == {2: 1, 3: 2, 1: 3}
    assert rank_movement(1, ranks, previous) == "⬇️2"
    assert rank_movement(2, ranks, previous) == ""
    assert rank_movement(3, ranks, previous) == "New"
    with user_settings.get_connection() as connection:
        before_reads = list(connection.iterdump())
    assert get_competition_snapshot(123, data.tournament.id) == snapshot
    initialize_polls()
    assert get_competition_snapshot(123, data.tournament.id) == snapshot
    with user_settings.get_connection() as connection:
        assert list(connection.iterdump()) == before_reads


def test_rank_ties_ignore_submitted_count_when_correct_and_accuracy_are_equal(competition):
    data = competition(2)
    pick(data.matches[0], 1, False)
    for match in data.matches:
        pick(match, 2, False)
    complete(data)
    rows = get_competition_stats(123, data.tournament.id)
    assert competition_ranks(rows) == {1: 1, 2: 1}
    assert rows[0].ranking_key == rows[1].ranking_key


def test_snapshot_pending_and_scored_histories_are_isolated_and_ordered(competition):
    data = competition(7)
    for match in data.matches:
        pick(match, 7)
    states = [MatchStatus.COMPLETED, MatchStatus.COMPLETED, MatchStatus.LIVE,
              MatchStatus.SCHEDULED, MatchStatus.CANCELLED, MatchStatus.POSTPONED, MatchStatus.COMPLETED]
    updated = replace(data, matches=[replace(match, status=state, winner_id=match.team1_id if i < 2 else None)
                                    for i, (match, state) in enumerate(zip(data.matches, states))])
    reconcile_polls(123, updated, data.matches[-1].start_time + timedelta(hours=1))
    snapshot = get_competition_snapshot(123, data.tournament.id, data=updated)
    assert snapshot.eligible == 2
    row, = snapshot.rows
    assert (row.correct, row.submitted, row.current_streak, row.longest_streak) == (2, 2, 2, 2)
    assert [prediction.match_id for prediction in snapshot.recent[7]] == ["m1", "m0"]
    assert [prediction.match_id for prediction in snapshot.pending[7]] == ["m2", "m3", "m5", "m6"]
    assert get_competition_snapshot(999, data.tournament.id).rows == []
    assert get_competition_snapshot(123, "other").pending == {}


def test_last_result_uses_confirmation_order_not_match_start_order(competition):
    data = competition(2)
    first, second = data.matches
    pick(first, 7)
    pick(second, 7, False)
    out_of_order = replace(data, matches=[first, replace(second, status=MatchStatus.COMPLETED, winner_id=second.team1_id)])
    reconcile_polls(123, out_of_order, second.start_time + timedelta(hours=1))
    old_rows = get_competition_stats(123, data.tournament.id)
    complete(data)
    snapshot = get_competition_snapshot(123, data.tournament.id)
    assert snapshot.previous_rows == old_rows


def test_no_historical_confirmation_order_does_not_invent_movement(competition):
    data = competition(1)
    pick(data.matches[0], 7)
    complete(data)
    with user_settings.get_connection() as connection:
        connection.execute("UPDATE match_polls SET result_observed_at = NULL")
        connection.commit()
    assert get_competition_snapshot(123, data.tournament.id).previous_rows is None


def team_series(competition, pairs):
    data = competition(len(pairs))
    data = replace(data, matches=[replace(match, team1_id=first, team2_id=second)
                                 for match, (first, second) in zip(data.matches, pairs)])
    reconcile_polls(123, data, data.matches[0].start_time - timedelta(hours=2))
    for match in data.matches:
        record_poll_opportunity(123, match.id, match.team1_id, match.team2_id, match.start_time - timedelta(hours=1))
    return data


def test_team_insights_read_both_sides_and_require_three_scored_predictions(competition):
    data = team_series(competition, [("T1", "G2"), ("T1", "GEN"), ("T1", "C9"), ("G2", "GEN"), ("G2", "C9")])
    for match, choice in zip(data.matches, ["T1", "GEN", "C9", "G2", "G2"]):
        cast_vote(123, match.id, 7, choice, match.start_time - timedelta(minutes=10))
    completed = complete(data)
    snapshot = get_competition_snapshot(123, data.tournament.id, data=completed)
    insights = snapshot.advanced[7]
    assert (insights.best_read.team_id, insights.best_read.correct, insights.best_read.scored) == ("G2", 3, 3)
    assert (insights.hardest.team_id, insights.hardest.correct, insights.hardest.scored) == ("T1", 1, 3)
    assert (insights.most_backed.team_id, insights.most_backed.backed, insights.most_backed.scored, insights.most_backed.backed_wins) == ("G2", 2, 3, 2)
    assert snapshot.rows[0].correct == 3 and snapshot.rows[0].submitted == 5


def test_most_backed_wins_only_counts_successful_backings_not_all_team_wins(competition):
    data = team_series(competition, [("G2", "C9")] * 5)
    for i, match in enumerate(data.matches):
        cast_vote(123, match.id, 7, "G2" if i < 4 else "C9", match.start_time - timedelta(minutes=10))
    completed = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id="C9" if i == 3 else "G2")
                                      for i, match in enumerate(data.matches)])
    reconcile_polls(123, completed, data.matches[-1].start_time + timedelta(hours=2))
    stats = get_competition_snapshot(123, data.tournament.id).advanced[7].most_backed
    assert (stats.team_id, stats.backed, stats.scored, stats.backed_wins) == ("G2", 4, 5, 3)


def test_hopium_supplier_counts_losing_backings_instead_of_total_backings(competition):
    data = team_series(competition, [("T1", "C9")] * 4 + [("G2", "C9")] * 2)
    for match in data.matches:
        cast_vote(123, match.id, 7, match.team1_id, match.start_time - timedelta(minutes=10))
    completed = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED,
                                               winner_id="T1" if i < 4 else "C9")
                                      for i, match in enumerate(data.matches)])
    reconcile_polls(123, completed, data.matches[-1].start_time + timedelta(hours=2))
    stats = get_competition_snapshot(123, data.tournament.id).advanced[7]
    assert stats.most_backed.team_id == "T1"
    assert stats.hopium_supplier.team_id == "G2"
    assert stats.hopium_supplier.backed_losses == 2


def test_hopium_supplier_is_empty_when_all_backings_win(competition):
    data = team_series(competition, [("T1", "C9")] * 3)
    for match in data.matches:
        cast_vote(123, match.id, 7, "T1", match.start_time - timedelta(minutes=10))
    complete(data)
    assert get_competition_snapshot(123, data.tournament.id).advanced[7].hopium_supplier is None


def test_crowd_stats_exclude_even_splits_and_choose_lowest_correct_backing_share(competition):
    data = team_series(competition, [("T1", "C9")] * 5)
    votes = [
        [(7, "T1"), (8, "T1")] + [(user, "C9") for user in range(9, 15)],  # 2/8, correct
        [(7, "T1")] + [(user, "C9") for user in range(8, 11)],  # 1/4, below boldest threshold
        [(7, "C9"), (8, "C9"), (9, "T1"), (10, "T1")],  # even split
        [(7, "C9")] + [(user, "T1") for user in range(8, 13)],  # 1/6, incorrect
        [(7, "T1")] + [(user, "C9") for user in range(8, 13)],  # 1/6, correct, initially pending
    ]
    for match, choices in zip(data.matches, votes):
        for user, choice in choices:
            cast_vote(123, match.id, user, choice, match.start_time - timedelta(minutes=10))
    partial = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id="T1") if i < 4 else match
                                    for i, match in enumerate(data.matches)])
    reconcile_polls(123, partial, data.matches[-1].start_time - timedelta(minutes=8))
    first = get_competition_snapshot(123, data.tournament.id, data=partial)
    stats = first.advanced[7]
    assert (stats.minority_correct, stats.minority_total) == (2, 3)
    assert (stats.boldest.prediction.match_id, stats.boldest.backing, stats.boldest.voters) == ("m0", 2, 8)
    completed = complete(data)
    snapshot = get_competition_snapshot(123, data.tournament.id, data=completed)
    stats = snapshot.advanced[7]
    assert (stats.minority_correct, stats.minority_total) == (3, 4)
    assert (stats.boldest.prediction.match_id, stats.boldest.backing, stats.boldest.voters) == ("m4", 1, 6)
    initialize_polls()
    assert get_competition_snapshot(123, data.tournament.id) == snapshot
    assert get_competition_snapshot(999, data.tournament.id).advanced == {}
    assert get_competition_snapshot(123, "other").advanced == {}


def test_late_and_changed_pair_votes_do_not_inflate_crowd_qualification(competition):
    data = team_series(competition, [("T1", "C9")])
    match, = data.matches
    for user in range(7, 13):
        cast_vote(123, match.id, user, "T1" if user == 7 else "C9", match.start_time - timedelta(minutes=10))
    with user_settings.get_connection() as connection:
        connection.execute("UPDATE poll_votes SET updated_at = ? WHERE user_id = 11", ((match.start_time - timedelta(minutes=5)).isoformat(),))
        connection.execute("UPDATE poll_votes SET vote_team2_id = 'OLD' WHERE user_id = 12")
        connection.commit()
    complete(data)
    snapshot = get_competition_snapshot(123, data.tournament.id)
    stats = snapshot.advanced[7]
    assert (stats.minority_correct, stats.minority_total) == (1, 1)
    assert stats.boldest is None  # Only four valid voters, not six.
    assert stats.best_read is None and stats.hardest is None
    assert 11 not in snapshot.advanced and 12 not in snapshot.advanced


def test_advanced_stats_ignore_pending_cancelled_unpublished_and_unresolved_matches(competition):
    data = team_series(competition, [("T1", "C9")] * 5)
    for match in data.matches:
        cast_vote(123, match.id, 7, "T1", match.start_time - timedelta(minutes=10))
    partial = replace(data, matches=[replace(match, status=status, winner_id="T1" if i < 2 else None)
                                    for i, (match, status) in enumerate(zip(data.matches, [MatchStatus.COMPLETED,
                                    MatchStatus.COMPLETED, MatchStatus.LIVE, MatchStatus.CANCELLED, MatchStatus.COMPLETED]))])
    reconcile_polls(123, partial, data.matches[-1].start_time + timedelta(hours=1))
    with user_settings.get_connection() as connection:
        connection.execute("UPDATE match_polls SET published_at = NULL WHERE match_id = 'm1'")
        connection.commit()
    snapshot = get_competition_snapshot(123, data.tournament.id, data=partial)
    stats = snapshot.advanced[7]
    assert (stats.most_backed.backed, stats.most_backed.scored, stats.most_backed.backed_wins) == (1, 1, 1)
    assert stats.best_read is None and stats.hardest is None
    assert stats.minority_total == 0 and stats.boldest is None


@pytest.mark.parametrize("voters,qualifies", [(4, False), (5, True)])
def test_boldest_call_minimum_voter_boundary(competition, voters, qualifies):
    data = team_series(competition, [("T1", "C9")])
    match, = data.matches
    for user in range(7, 7 + voters):
        cast_vote(123, match.id, user, "T1" if user == 7 else "C9", match.start_time - timedelta(minutes=10))
    complete(data)
    stats = get_competition_snapshot(123, data.tournament.id).advanced[7]
    assert (stats.boldest is not None) is qualifies


def test_swiss_scope_excludes_playins_from_totals_ranks_history_and_team_insights(competition):
    data = competition(4)
    swiss = next(stage for stage in data.stages if "swiss" in stage.name.casefold())
    knockout = next(stage for stage in data.stages if stage.order > swiss.order)
    matches = [data.matches[0], replace(data.matches[1], stage_id=swiss.id),
               replace(data.matches[2], stage_id=knockout.id), replace(data.matches[3], stage_id=swiss.id)]
    data = replace(data, matches=matches)
    for index, match in enumerate(matches):
        pick(match, 7, index != 1)
    pick(matches[0], 8)  # A Play-ins-only user must not be ranked.
    completed = replace(data, matches=[replace(match, status=MatchStatus.COMPLETED, winner_id=match.team1_id)
                                      if i < 3 else match for i, match in enumerate(matches)])
    reconcile_polls(123, completed, matches[-1].start_time - timedelta(minutes=7))
    snapshot = get_competition_snapshot(123, data.tournament.id, data=completed, from_swiss=True)
    row, = snapshot.rows
    assert (row.user_id, row.correct, row.submitted, row.eligible) == (7, 1, 2, 2)
    assert competition_ranks(snapshot.rows) == {7: 1}
    assert [prediction.match_id for prediction in snapshot.recent[7]] == ["m2", "m1"]
    assert [prediction.match_id for prediction in snapshot.pending[7]] == ["m3"]
    assert snapshot.advanced[7].best_read is None  # The Play-ins pick cannot make three.
    assert snapshot.advanced[7].most_backed.scored == 2
    assert 8 not in snapshot.advanced
    assert all(row.user_id != 8 for row in snapshot.previous_rows)
    initialize_polls()
    assert get_competition_snapshot(123, data.tournament.id, data=completed, from_swiss=True) == snapshot


def test_swiss_scope_does_not_guess_unknown_stage_data(competition):
    data = competition(1)
    pick(data.matches[0], 7)
    completed = complete(data)
    assert get_competition_snapshot(123, data.tournament.id, data=completed, from_swiss=True).rows == []
    assert get_competition_snapshot(123, data.tournament.id, from_swiss=True).rows == []
    unknown = replace(completed, matches=[replace(completed.matches[0], stage_id="unknown")])
    assert get_competition_snapshot(123, data.tournament.id, data=unknown, from_swiss=True).rows == []
