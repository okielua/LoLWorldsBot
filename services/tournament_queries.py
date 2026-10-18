from datetime import date, datetime
from zoneinfo import ZoneInfo

from models import Match, MatchStatus, Standing
from services.tournament_data import TournamentData


def get_matches_for_local_date(data: TournamentData, local_date: date, timezone_name: str) -> list[Match]:
    zone = ZoneInfo(timezone_name)
    return sorted(
        (match for match in data.matches if match.start_time.astimezone(zone).date() == local_date),
        key=lambda match: match.start_time,
    )


def get_matches_for_team(
    data: TournamentData,
    team_id: str,
) -> list[Match]:
    from services.spoilers import current_policy
    policy = current_policy()
    return [
        match
        for match in data.matches
        if team_id in {
            match.team1_id,
            match.team2_id,
        }
        and not (policy and policy.participant_protected(match, 1 if match.team1_id == team_id else 2))
    ]


def get_matches_for_stage(
    data: TournamentData,
    stage_id: str,
) -> list[Match]:
    return [
        match
        for match in data.matches
        if match.stage_id == stage_id
    ]


def get_next_match(
    data: TournamentData,
    now: datetime,
) -> Match | None:
    upcoming = [
        match
        for match in data.matches
        if match.status == MatchStatus.SCHEDULED and match.start_time > now
    ]

    if not upcoming:
        return None

    return min(
        upcoming,
        key=lambda match: match.start_time,
    )


def get_matches_for_votes(data, now, recent_completed_ids):
    next_match = get_next_match(data, now)
    return sorted(
        (match for match in data.matches
         if match == next_match or match.status == MatchStatus.LIVE
         or (match.status == MatchStatus.COMPLETED and match.id in recent_completed_ids)),
        key=lambda match: match.start_time,
    )


def get_previous_matches(data: TournamentData, now: datetime, limit: int = 2) -> list[Match]:
    """Newest completed series first; never treat a past start time as a result."""
    return sorted(
        (match for match in data.matches if match.status == MatchStatus.COMPLETED and match.start_time <= now),
        key=lambda match: match.start_time, reverse=True,
    )[:limit]


def get_standings(
    data: TournamentData,
    stage_id: str,
) -> list[Standing]:
    standings = [
        standing
        for standing in data.standings
        if standing.stage_id == stage_id
    ]

    return sorted(
        standings,
        key=lambda standing: (
            standing.rank is None,
            standing.rank
            if standing.rank is not None
            else 999,
            -standing.wins,
            standing.losses,
        ),
    )
