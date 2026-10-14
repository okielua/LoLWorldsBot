from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

PUBLIC_REMINDER_MINUTES = 5
PERSONAL_REMINDER_OPTIONS = {0, 15, 30, 60}


class ReminderType(str, Enum):
    PUBLIC = "public"
    PERSONAL = "personal"


@dataclass(frozen=True)
class ReminderKey:
    match_id: str
    reminder_type: ReminderType
    offset_minutes: int
    user_id: int | None = None


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

    window_end = trigger_time + timedelta(seconds=window_seconds)

    return trigger_time <= now < window_end


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
