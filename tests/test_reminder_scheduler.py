from datetime import UTC, datetime, timedelta

import pytest

from models import Match, MatchStatus
from services.reminder_engine import (
    ReminderTracker,
    UserReminderPreferences,
)
from services.reminder_scheduler import (
    check_reminders,
)

MATCH_START = datetime(
    2026,
    10,
    20,
    18,
    0,
    tzinfo=UTC,
)


def make_match():
    return Match(
        id="test-match",
        tournament_id="worlds-2026",
        stage_id="swiss",
        team1_id="T1",
        team2_id="G2",
        start_time=MATCH_START,
        best_of=1,
    )


@pytest.mark.parametrize("status", [s for s in MatchStatus if s != MatchStatus.SCHEDULED])
@pytest.mark.parametrize("minutes", [5, 0, 15, 30, 60])
def test_non_scheduled_matches_never_generate_reminders(status, minutes):
    match = make_match()
    match.status = status
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        all_games_server_ping=True,
        dm_enabled=True,
        dm_reminder_minutes=minutes if minutes != 5 else 0,
    )
    tracker = ReminderTracker()
    now = MATCH_START - timedelta(minutes=minutes)

    result = check_reminders([match], [user], now, tracker)

    assert result.server_reminders == ()
    assert result.dm_reminders == ()
    # Skipping an ineligible match must not consume its reminder keys.
    match.status = MatchStatus.SCHEDULED
    eligible_result = check_reminders([match], [user], now, tracker)
    if minutes == 5:
        assert len(eligible_result.server_reminders) == 1
    else:
        assert len(eligible_result.dm_reminders) == 1


def test_server_reminder_fires_five_minutes_before():
    match = make_match()

    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        followed_team_server_ping=True,
    )

    tracker = ReminderTracker()

    result = check_reminders(
        matches=[match],
        users=[user],
        now=MATCH_START - timedelta(minutes=5),
        tracker=tracker,
    )

    assert len(result.server_reminders) == 1

    assert result.server_reminders[0].ping_user_ids == frozenset({1})


def test_delivery_candidates_do_not_consume_keys_before_success():
    tracker = ReminderTracker()
    match = make_match()
    now = MATCH_START - timedelta(minutes=5)
    first = check_reminders([match], [], now, tracker, mark_sent=False)
    second = check_reminders([match], [], now, tracker, mark_sent=False)
    assert len(first.server_reminders) == 1
    assert second == first


def test_server_reminder_still_exists_with_no_pings():
    match = make_match()

    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset(),
        all_games_server_ping=False,
        followed_team_server_ping=False,
    )

    tracker = ReminderTracker()

    result = check_reminders(
        matches=[match],
        users=[user],
        now=MATCH_START - timedelta(minutes=5),
        tracker=tracker,
    )

    assert len(result.server_reminders) == 1

    assert result.server_reminders[0].ping_user_ids == frozenset()


def test_server_reminder_is_not_created_twice():
    match = make_match()

    tracker = ReminderTracker()

    now = MATCH_START - timedelta(minutes=5)

    first = check_reminders(
        matches=[match],
        users=[],
        now=now,
        tracker=tracker,
    )

    second = check_reminders(
        matches=[match],
        users=[],
        now=now,
        tracker=tracker,
    )

    assert len(first.server_reminders) == 1

    assert len(second.server_reminders) == 0


def test_dm_fires_at_users_selected_time():
    match = make_match()

    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        dm_enabled=True,
        dm_reminder_minutes=15,
    )

    tracker = ReminderTracker()

    result = check_reminders(
        matches=[match],
        users=[user],
        now=MATCH_START - timedelta(minutes=15),
        tracker=tracker,
    )

    assert len(result.dm_reminders) == 1

    assert result.dm_reminders[0].user_id == 1

    assert result.dm_reminders[0].reminder_minutes == 15


def test_dm_does_not_fire_at_wrong_time():
    match = make_match()

    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        dm_enabled=True,
        dm_reminder_minutes=30,
    )

    tracker = ReminderTracker()

    result = check_reminders(
        matches=[match],
        users=[user],
        now=MATCH_START - timedelta(minutes=15),
        tracker=tracker,
    )

    assert len(result.dm_reminders) == 0


def test_dm_is_not_created_twice():
    match = make_match()

    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        dm_enabled=True,
        dm_reminder_minutes=15,
    )

    tracker = ReminderTracker()

    now = MATCH_START - timedelta(minutes=15)

    first = check_reminders(
        matches=[match],
        users=[user],
        now=now,
        tracker=tracker,
    )

    second = check_reminders(
        matches=[match],
        users=[user],
        now=now,
        tracker=tracker,
    )

    assert len(first.dm_reminders) == 1

    assert len(second.dm_reminders) == 0
