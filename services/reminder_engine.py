from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

SERVER_REMINDER_MINUTES = 5
SERVER_RECOVERY_WINDOW_SECONDS = SERVER_REMINDER_MINUTES * 60
DM_REMINDER_OPTIONS = {0, 15, 30, 60}


class ReminderType(str, Enum):
    SERVER = "server"
    DM = "dm"


@dataclass(frozen=True)
class ReminderKey:
    match_id: str
    reminder_type: ReminderType
    user_id: int | None = None


@dataclass(frozen=True)
class UserReminderPreferences:
    user_id: int
    followed_team_ids: frozenset[str]

    all_games_server_ping: bool = False  # Legacy flag; all-game pings now use the Worlds role.
    followed_team_server_ping: bool = True

    dm_enabled: bool = False
    dm_reminder_minutes: int = 0


def get_reminder_time(
    match_start: datetime,
    offset_minutes: int,
) -> datetime:
    if match_start.tzinfo is None:
        raise ValueError("match_start must be timezone-aware.")

    return match_start - timedelta(minutes=offset_minutes)


def is_reminder_due(
    match_start: datetime,
    now: datetime,
    offset_minutes: int,
    window_seconds: int = 60,
) -> bool:
    if match_start.tzinfo is None:
        raise ValueError("match_start must be timezone-aware.")

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")

    trigger_time = get_reminder_time(
        match_start,
        offset_minutes,
    )

    return trigger_time <= now < trigger_time + timedelta(seconds=window_seconds)


def follows_match(
    preferences: UserReminderPreferences,
    team1_id: str,
    team2_id: str,
) -> bool:
    return bool(
        preferences.followed_team_ids
        & {
            team1_id,
            team2_id,
        }
    )


def get_server_ping_user_ids(
    users: list[UserReminderPreferences],
    team1_id: str,
    team2_id: str,
) -> set[int]:
    ping_user_ids = set()

    for user in users:
        if user.followed_team_server_ping and follows_match(
            user,
            team1_id,
            team2_id,
        ):
            ping_user_ids.add(user.user_id)

    return ping_user_ids


def get_dm_user_ids(
    users: list[UserReminderPreferences],
    team1_id: str,
    team2_id: str,
    reminder_minutes: int,
) -> set[int]:
    dm_user_ids = set()

    for user in users:
        if not user.dm_enabled:
            continue

        if user.dm_reminder_minutes != reminder_minutes:
            continue

        if not follows_match(
            user,
            team1_id,
            team2_id,
        ):
            continue

        dm_user_ids.add(user.user_id)

    return dm_user_ids


class ReminderTracker:
    def __init__(self):
        self._sent: set[ReminderKey] = set()

    def has_sent(
        self,
        key: ReminderKey,
    ) -> bool:
        return key in self._sent

    def mark_sent(
        self,
        key: ReminderKey,
    ) -> None:
        self._sent.add(key)

    def clear(self) -> None:
        self._sent.clear()

    def forget_match(self, match_id: str) -> None:
        self._sent = {key for key in self._sent if key.match_id != match_id}
