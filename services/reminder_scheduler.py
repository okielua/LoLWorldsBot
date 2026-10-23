from dataclasses import dataclass
from datetime import datetime

from models import Match, MatchStatus
from services.reminder_engine import (
    DM_REMINDER_OPTIONS,
    ReminderKey,
    ReminderTracker,
    ReminderType,
    UserReminderPreferences,
    get_dm_user_ids,
    get_server_ping_user_ids,
    is_reminder_due,
    public_reminder_due,
)


@dataclass(frozen=True)
class ServerReminderEvent:
    match_id: str
    team1_id: str
    team2_id: str
    ping_user_ids: frozenset[int]


@dataclass(frozen=True)
class DMReminderEvent:
    match_id: str
    user_id: int
    reminder_minutes: int


@dataclass(frozen=True)
class ReminderCheckResult:
    server_reminders: tuple[ServerReminderEvent, ...]
    dm_reminders: tuple[DMReminderEvent, ...]


def check_reminders(
    matches: list[Match],
    users: list[UserReminderPreferences],
    now: datetime,
    tracker: ReminderTracker,
    *,
    mark_sent: bool = True,
    include_dm: bool = True,
    server_window_seconds: int = 60,
) -> ReminderCheckResult:
    server_reminders = []
    dm_reminders = []

    for match in matches:
        if match.status not in {MatchStatus.SCHEDULED, MatchStatus.LIVE}:
            continue
        if "TBD" in {match.team1_id, match.team2_id}:
            continue

        server_key = ReminderKey(
            match_id=match.id,
            reminder_type=ReminderType.SERVER,
        )

        if not tracker.has_sent(server_key) and public_reminder_due(match, now, server_window_seconds):
            ping_user_ids = get_server_ping_user_ids(
                users,
                match.team1_id,
                match.team2_id,
            )

            server_reminders.append(
                ServerReminderEvent(
                    match_id=match.id,
                    team1_id=match.team1_id,
                    team2_id=match.team2_id,
                    ping_user_ids=frozenset(ping_user_ids),
                )
            )

            if mark_sent:
                tracker.mark_sent(server_key)

        if not include_dm or match.status != MatchStatus.SCHEDULED:
            continue

        for reminder_minutes in DM_REMINDER_OPTIONS:
            if not is_reminder_due(
                match.start_time,
                now,
                reminder_minutes,
            ):
                continue

            dm_user_ids = get_dm_user_ids(
                users,
                match.team1_id,
                match.team2_id,
                reminder_minutes,
            )

            for user_id in dm_user_ids:
                dm_key = ReminderKey(
                    match_id=match.id,
                    reminder_type=ReminderType.DM,
                    user_id=user_id,
                )

                if tracker.has_sent(dm_key):
                    continue

                dm_reminders.append(
                    DMReminderEvent(
                        match_id=match.id,
                        user_id=user_id,
                        reminder_minutes=reminder_minutes,
                    )
                )

                if mark_sent:
                    tracker.mark_sent(dm_key)

    return ReminderCheckResult(
        server_reminders=tuple(server_reminders),
        dm_reminders=tuple(dm_reminders),
    )
