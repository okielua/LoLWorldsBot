"""Derived competition statistics from published opportunities and confirmed results."""

from contextlib import closing
from dataclasses import dataclass
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
        return (self.correct, self.accuracy, self.submitted)


def get_competition_stats(guild_id, tournament_id, *, match_day=None):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN")
        polls = connection.execute("""
            SELECT match_id, team1_id, team2_id, start_time, winner_id, published_at
            FROM match_polls WHERE guild_id = ? AND tournament_id = ? AND winner_id IS NOT NULL
        """, (guild_id, tournament_id)).fetchall()
        votes = connection.execute("""
            SELECT votes.match_id, votes.user_id, votes.team_id, votes.updated_at,
                   votes.vote_team1_id, votes.vote_team2_id
            FROM poll_votes AS votes JOIN match_polls AS polls USING (guild_id, match_id)
            WHERE polls.guild_id = ? AND polls.tournament_id = ?
        """, (guild_id, tournament_id)).fetchall()
    eligible = []
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    for match_id, first, second, start, winner, published in polls:
        start = datetime.fromisoformat(start)
        if winner not in {first, second} or not published:
            continue
        if datetime.fromisoformat(published) >= start - timedelta(minutes=5):
            continue
        if match_day is not None and start.astimezone(zone).date().isoformat() != match_day:
            continue
        eligible.append((start, match_id, first, second, winner))
    eligible.sort(key=lambda row: (row[0], row[1]))
    by_match = {row[1]: row for row in eligible}
    by_user = {}
    for match_id, user_id, chosen, updated, first, second in votes:
        match = by_match.get(match_id)
        if (match is None or chosen not in set(match[2:4])
                or {first, second} != set(match[2:4])
                or datetime.fromisoformat(updated) >= match[0] - timedelta(minutes=5)):
            continue
        by_user.setdefault(user_id, {})[match_id] = chosen
    result = []
    for user_id, picks in by_user.items():
        correct = streak = best = 0
        for _, match_id, _, _, winner in eligible:
            if match_id not in picks:
                continue
            if picks[match_id] == winner:
                correct += 1
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        result.append(PredictionStats(user_id, correct, len(picks), len(eligible), streak, best))
    return sorted(result, key=lambda row: (-row.correct, -row.accuracy, -row.submitted, row.user_id))


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
