from datetime import datetime

from models import Match, Standing
from services.tournament_data import TournamentData


def get_matches_for_team(
    data: TournamentData,
    team_id: str,
) -> list[Match]:
    return [
        match
        for match in data.matches
        if team_id in {
            match.team1_id,
            match.team2_id,
        }
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
        if match.start_time > now
    ]

    if not upcoming:
        return None

    return min(
        upcoming,
        key=lambda match: match.start_time,
    )


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