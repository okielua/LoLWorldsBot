import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from models import (
    Match,
    MatchStatus,
    Stage,
    Standing,
    Team,
    Tournament,
    TournamentStatus,
)


@dataclass
class TournamentData:
    tournament: Tournament
    stages: list[Stage]
    teams: list[Team]
    matches: list[Match]
    standings: list[Standing]

    @property
    def teams_by_id(self) -> dict[str, Team]:
        return {team.id: team for team in self.teams}

    @property
    def stages_by_id(self) -> dict[str, Stage]:
        return {stage.id: stage for stage in self.stages}

    @property
    def matches_by_id(self) -> dict[str, Match]:
        return {match.id: match for match in self.matches}


def parse_datetime(value: str) -> datetime:
    # Convert JSON timestamps like:
    # 2025-10-15T05:00:00Z
    # into timezone-aware Python datetimes.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        raise ValueError(
            f"Datetime must be timezone-aware: {value}"
        )

    return parsed


def load_tournament_data(path: str | Path) -> TournamentData:
    path = Path(path)

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:
        raw = json.load(file)

    tournament_raw = raw["tournament"]

    tournament = Tournament(
        id=tournament_raw["id"],
        name=tournament_raw["name"],
        year=tournament_raw["year"],
        status=TournamentStatus(
            tournament_raw["status"]
        ),
        start_date=date.fromisoformat(
            tournament_raw["start_date"]
        ),
        end_date=date.fromisoformat(
            tournament_raw["end_date"]
        ),
    )

    stages = [
        Stage(
            id=item["id"],
            tournament_id=item["tournament_id"],
            name=item["name"],
            order=item["order"],
            format=item.get("format"),
        )
        for item in raw["stages"]
    ]

    teams = [
        Team(
            id=item["id"],
            name=item["name"],
            short_name=item["short_name"],
            region=item.get("region"),
            logo_url=item.get("logo_url"),
        )
        for item in raw["teams"]
    ]

    matches = [
        Match(
            id=item["id"],
            tournament_id=item["tournament_id"],
            stage_id=item["stage_id"],
            team1_id=item["team1_id"],
            team2_id=item["team2_id"],
            start_time=parse_datetime(
                item["start_time"]
            ),
            best_of=item["best_of"],
            round_name=item.get("round_name"),
            status=MatchStatus(
                item["status"]
            ),
            team1_score=item.get(
                "team1_score"
            ),
            team2_score=item.get(
                "team2_score"
            ),
            winner_id=item.get(
                "winner_id"
            ),
        )
        for item in raw["matches"]
    ]

    standings = [
        Standing(
            tournament_id=item[
                "tournament_id"
            ],
            stage_id=item["stage_id"],
            team_id=item["team_id"],
            wins=item.get("wins", 0),
            losses=item.get("losses", 0),
            rank=item.get("rank"),
        )
        for item in raw.get(
            "standings",
            [],
        )
    ]

    data = TournamentData(
        tournament=tournament,
        stages=stages,
        teams=teams,
        matches=matches,
        standings=standings,
    )

    validate_tournament_data(data)

    return data


def validate_tournament_data(
    data: TournamentData,
) -> None:
    team_ids = {
        team.id
        for team in data.teams
    }

    stage_ids = {
        stage.id
        for stage in data.stages
    }

    for stage in data.stages:
        if (
            stage.tournament_id
            != data.tournament.id
        ):
            raise ValueError(
                f"Stage {stage.id} references "
                "the wrong tournament."
            )

    for match in data.matches:
        if match.stage_id not in stage_ids:
            raise ValueError(
                f"Match {match.id} references "
                f"unknown stage {match.stage_id}."
            )

        if match.team1_id not in team_ids:
            raise ValueError(
                f"Match {match.id} references "
                f"unknown team {match.team1_id}."
            )

        if match.team2_id not in team_ids:
            raise ValueError(
                f"Match {match.id} references "
                f"unknown team {match.team2_id}."
            )

        if (
            match.winner_id is not None
            and match.winner_id
            not in {
                match.team1_id,
                match.team2_id,
            }
        ):
            raise ValueError(
                f"Match {match.id} has invalid "
                f"winner {match.winner_id}."
            )

    for standing in data.standings:
        if standing.stage_id not in stage_ids:
            raise ValueError(
                "Standing references unknown "
                f"stage {standing.stage_id}."
            )

        if standing.team_id not in team_ids:
            raise ValueError(
                "Standing references unknown "
                f"team {standing.team_id}."
            )