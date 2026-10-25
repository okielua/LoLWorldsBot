"""Derived competition statistics from published opportunities and confirmed results."""

from contextlib import closing
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

from models import MatchStatus
from services.match_times import TOURNAMENT_TIMEZONE
from services.user_settings import get_connection


@dataclass(frozen=True)
class PredictionStats:
    user_id: int
    correct: int
    submitted: int
    eligible: int
    current_streak: int
    longest_streak: int

    @property
    def accuracy(self):
        return Fraction(self.correct, self.submitted) if self.submitted else Fraction(0)

    @property
    def participation(self):
        return Fraction(self.submitted, self.eligible) if self.eligible else Fraction(0)

    @property
    def ranking_key(self):
        return (self.correct, self.accuracy)


@dataclass(frozen=True)
class Prediction:
    match_id: str
    team1_id: str
    team2_id: str
    start_time: datetime
    chosen: str
    winner_id: str | None


@dataclass(frozen=True)
class TeamPredictionStats:
    team_id: str
    correct: int
    scored: int
    backed: int
    backed_wins: int

    @property
    def backed_losses(self):
        return self.backed - self.backed_wins

    @property
    def accuracy(self):
        return Fraction(self.correct, self.scored)


@dataclass(frozen=True)
class MinorityCall:
    prediction: Prediction
    backing: int
    voters: int

    @property
    def backing_share(self):
        return Fraction(self.backing, self.voters)


@dataclass(frozen=True)
class AdvancedPredictionStats:
    best_read: TeamPredictionStats | None = None
    hardest: TeamPredictionStats | None = None
    most_backed: TeamPredictionStats | None = None
    minority_correct: int = 0
    minority_total: int = 0
    boldest: MinorityCall | None = None
    hopium_supplier: TeamPredictionStats | None = None


@dataclass(frozen=True)
class CompetitionSnapshot:
    rows: list
    previous_rows: list | None
    recent: dict
    pending: dict
    eligible: int
    advanced: dict = field(default_factory=dict)
    result_match_ids: tuple | None = None


def advanced_prediction_stats(eligible, by_user):
    """Use the same validated scored votes as accuracy, including final changes."""
    matches = {match[1]: match for match in eligible}
    crowd = {match_id: Counter() for match_id in matches}
    for picks in by_user.values():
        for match_id, chosen in picks.items():
            crowd[match_id][chosen] += 1
    result = {}
    for user_id, picks in by_user.items():
        teams, minority_correct, minority_total, calls = {}, 0, 0, []
        for match_id, chosen in picks.items():
            start, _, first, second, winner = matches[match_id]
            correct = chosen == winner
            for team_id in (first, second):
                counts = teams.setdefault(team_id, [0, 0, 0, 0])
                counts[0] += int(correct)
                counts[1] += 1
                counts[2] += int(chosen == team_id)
                counts[3] += int(chosen == team_id and correct)
            backing, voters = crowd[match_id][chosen], sum(crowd[match_id].values())
            if backing * 2 < voters:
                minority_total += 1
                minority_correct += int(correct)
                if correct and voters >= 5:
                    calls.append(MinorityCall(Prediction(match_id, first, second, start, chosen, winner), backing, voters))
        teams = [TeamPredictionStats(team_id, *counts) for team_id, counts in teams.items()]
        qualified = [team for team in teams if team.scored >= 3]
        # Equal accuracies prefer the larger sample; remaining ties use team ID.
        best = min(qualified, key=lambda team: (-team.accuracy, -team.scored, team.team_id), default=None)
        hardest = min(qualified, key=lambda team: (team.accuracy, -team.scored, team.team_id), default=None)
        backed = min(teams, key=lambda team: (-team.backed, -team.scored, team.team_id), default=None)
        hopium = min((team for team in teams if team.backed_losses > 0),
                     key=lambda team: (-team.backed_losses, team.team_id), default=None)
        # For equally rare calls, prefer more voters, then the newest match.
        boldest = min(calls, key=lambda call: (call.backing_share, -call.voters,
                                             -call.prediction.start_time.timestamp(), call.prediction.match_id), default=None)
        result[user_id] = AdvancedPredictionStats(best, hardest, backed, minority_correct, minority_total, boldest,
                                                 hopium_supplier=hopium)
    return result


def competition_ranks(rows):
    result, previous, rank = {}, None, 0
    for index, row in enumerate(sorted(rows, key=lambda row: (-row.correct, -row.accuracy, row.user_id)), 1):
        if row.ranking_key != previous:
            rank = index
        previous = row.ranking_key
        result[row.user_id] = rank
    return result


def get_competition_snapshot(guild_id, tournament_id, *, match_day=None, data=None, from_swiss=False, match_ids=None):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN")
        polls = connection.execute("""
            SELECT match_id, team1_id, team2_id, start_time, winner_id, published_at, result_observed_at, voting_closes_at
            FROM match_polls WHERE guild_id = ? AND tournament_id = ?
        """, (guild_id, tournament_id)).fetchall()
        votes = connection.execute("""
            SELECT votes.match_id, votes.user_id, votes.team_id, votes.updated_at,
                   votes.vote_team1_id, votes.vote_team2_id
            FROM poll_votes AS votes JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE polls.guild_id = ? AND polls.tournament_id = ?
        """, (guild_id, tournament_id)).fetchall()
    eligible = []
    swiss = next((stage for stage in data.stages if "swiss" in stage.name.casefold()), None) if data else None

    def in_scope(match_id):
        if match_ids is not None and match_id not in match_ids:
            return False
        if not from_swiss:
            return True
        match = data.matches_by_id.get(match_id) if data else None
        stage = data.stages_by_id.get(match.stage_id) if match else None
        return bool(swiss and stage and stage.order >= swiss.order)

    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    observed = {}
    poll_by_id = {poll[0]: poll for poll in polls}
    for match_id, first, second, start, winner, published, confirmed, cutoff in polls:
        if not in_scope(match_id):
            continue
        start = datetime.fromisoformat(start)
        source = data.matches_by_id.get(match_id) if data else None
        if source and (source.status != MatchStatus.COMPLETED or source.winner_id != winner):
            continue
        if winner not in {first, second} or not published:
            continue
        if datetime.fromisoformat(published) >= (datetime.fromisoformat(cutoff) if cutoff else start - timedelta(minutes=5)):
            continue
        if match_day is not None and start.astimezone(zone).date().isoformat() != match_day:
            continue
        eligible.append((start, match_id, first, second, winner))
        if confirmed:
            observed[match_id] = datetime.fromisoformat(confirmed)
    eligible.sort(key=lambda row: (row[0], row[1]))
    by_match = {row[1]: row for row in eligible}
    by_user, pending, recent = {}, {}, {}
    for match_id, user_id, chosen, updated, first, second in votes:
        if not in_scope(match_id):
            continue
        match = by_match.get(match_id)
        poll = poll_by_id[match_id]
        start = datetime.fromisoformat(poll[3])
        deadline = datetime.fromisoformat(poll[7]) if poll[7] else start - timedelta(minutes=5)
        source = data.matches_by_id.get(match_id) if data else None
        valid = (chosen in {poll[1], poll[2]} and {first, second} == {poll[1], poll[2]}
                 and datetime.fromisoformat(updated) < deadline)
        unresolved = poll[4] is None or (source is not None and (source.status != MatchStatus.COMPLETED or source.winner_id not in {poll[1], poll[2]}))
        if (valid and unresolved and (source is None or source.status != MatchStatus.CANCELLED)):
            pending.setdefault(user_id, []).append(Prediction(match_id, poll[1], poll[2], start, chosen, None))
        if (match is None or chosen not in set(match[2:4])
                or {first, second} != set(match[2:4])
                or datetime.fromisoformat(updated) >= deadline):
            continue
        by_user.setdefault(user_id, {})[match_id] = chosen
        recent.setdefault(user_id, []).append(Prediction(match_id, first, second, start, chosen, match[4]))

    def calculate(matches):
        result = []
        ids = {row[1] for row in matches}
        for user_id, picks in by_user.items():
            submitted = len(ids.intersection(picks))
            if not submitted:
                continue
            correct = streak = best = 0
            for _, match_id, _, _, winner in matches:
                if match_id not in picks:
                    continue
                if picks[match_id] == winner:
                    correct += 1
                    streak += 1
                    best = max(best, streak)
                else:
                    streak = 0
            result.append(PredictionStats(user_id, correct, submitted, len(matches), streak, best))
        return sorted(result, key=lambda row: (-row.correct, -row.accuracy, row.user_id))

    # First-confirmation timestamps persist in SQLite. Removing the most recently
    # included result reconstructs the preceding ranking, independent of reads
    # and restarts. Batch confirmations have a stable match-order tie breaker.
    latest = max((row for row in eligible if row[1] in observed),
                 key=lambda row: (observed[row[1]], row[0], row[1]), default=None)
    previous = calculate([row for row in eligible if row[1] != latest[1]]) if latest else None
    for predictions in recent.values():
        predictions.sort(key=lambda row: (row.start_time, row.match_id), reverse=True)
    for predictions in pending.values():
        predictions.sort(key=lambda row: (row.start_time, row.match_id))
    return CompetitionSnapshot(calculate(eligible), previous, recent, pending, len(eligible),
                               advanced_prediction_stats(eligible, by_user), tuple(row[1] for row in eligible))


def get_competition_stats(guild_id, tournament_id, *, match_day=None):
    return get_competition_snapshot(guild_id, tournament_id, match_day=match_day).rows


def get_previous_day_mvp(guild_id, data, current_day):
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    days = {}
    for match in data.matches:
        if match.status in {MatchStatus.CANCELLED, MatchStatus.POSTPONED}:
            continue
        day = match.start_time.astimezone(zone).date().isoformat()
        if day < current_day:
            days.setdefault(day, []).append(match)
    if not days:
        return []
    previous_day = max(days)
    if any(match.status != MatchStatus.COMPLETED or match.winner_id not in {match.team1_id, match.team2_id}
           for match in days[previous_day]):
        return []
    rows = get_competition_stats(guild_id, data.tournament.id, match_day=previous_day)
    if not rows or rows[0].correct == 0:
        return []
    return [row for row in rows if (row.correct, row.accuracy) == (rows[0].correct, rows[0].accuracy)]


def get_voter_display_names(guild_id, tournament_id):
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT votes.user_id, votes.display_name FROM poll_votes AS votes
            JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE polls.guild_id = ? AND polls.tournament_id = ? ORDER BY votes.updated_at
        """, (guild_id, tournament_id)).fetchall()
    return {user_id: name or f"User {user_id}" for user_id, name in rows}
