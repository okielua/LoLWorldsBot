from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class TournamentStatus(str, Enum):
    UPCOMING = "upcoming"
    ONGOING = "ongoing"
    COMPLETED = "completed"


class MatchStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class ReminderMode(str, Enum):
    SERVER = "server"
    DM = "dm"
    BOTH = "both"
    NONE = "none"


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    short_name: str
    region: str | None = None
    logo_url: str | None = None


@dataclass(frozen=True)
class Stage:
    id: str
    tournament_id: str
    name: str
    order: int
    format: str | None = None


@dataclass(frozen=True)
class Tournament:
    id: str
    name: str
    year: int
    status: TournamentStatus
    start_date: date
    end_date: date


@dataclass
class Match:
    id: str
    tournament_id: str
    stage_id: str

    team1_id: str
    team2_id: str

    start_time: datetime
    best_of: int

    round_name: str | None = None
    status: MatchStatus = MatchStatus.SCHEDULED

    team1_score: int | None = None
    team2_score: int | None = None
    winner_id: str | None = None

    def __post_init__(self):
        if self.start_time.tzinfo is None:
            raise ValueError(
                "Match start_time must be timezone-aware."
            )

        if self.best_of not in {1, 3, 5}:
            raise ValueError(
                "best_of must be 1, 3, or 5."
            )


@dataclass
class Standing:
    tournament_id: str
    stage_id: str
    team_id: str

    wins: int = 0
    losses: int = 0
    rank: int | None = None


@dataclass
class UserSubscription:
    user_id: int

    subscribed_to_all_matches: bool = False
    followed_team_ids: set[str] = field(default_factory=set)

    reminder_mode: ReminderMode = ReminderMode.SERVER
    reminder_minutes: int = 30


@dataclass
class Prediction:
    user_id: int
    match_id: str

    predicted_winner_id: str
    created_at: datetime

    points_awarded: int | None = None