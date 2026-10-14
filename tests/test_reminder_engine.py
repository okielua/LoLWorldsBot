from datetime import UTC, datetime, timedelta

import pytest

from services.reminder_engine import (
    ReminderKey,
    ReminderTracker,
    ReminderType,
    UserReminderPreferences,
    get_dm_user_ids,
    get_reminder_time,
    get_server_ping_user_ids,
    is_reminder_due,
)

MATCH_START = datetime(
    2026,
    10,
    20,
    18,
    0,
    tzinfo=UTC,
)


def test_get_five_minute_reminder_time():
    reminder_time = get_reminder_time(
        MATCH_START,
        5,
    )

    assert reminder_time == datetime(
        2026,
        10,
        20,
        17,
        55,
        tzinfo=UTC,
    )


def test_reminder_is_due():
    now = MATCH_START - timedelta(minutes=5)

    assert is_reminder_due(
        MATCH_START,
        now,
        5,
    )


def test_reminder_not_due_too_early():
    now = MATCH_START - timedelta(minutes=5) - timedelta(seconds=1)

    assert not is_reminder_due(
        MATCH_START,
        now,
        5,
    )


def test_all_games_user_gets_server_ping():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset(),
        all_games_server_ping=True,
    )

    result = get_server_ping_user_ids(
        [user],
        "T1",
        "G2",
    )

    assert result == {1}


def test_followed_team_user_gets_server_ping():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        followed_team_server_ping=True,
    )

    result = get_server_ping_user_ids(
        [user],
        "T1",
        "G2",
    )

    assert result == {1}


def test_unrelated_team_does_not_get_ping():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"GEN"}),
        followed_team_server_ping=True,
    )

    result = get_server_ping_user_ids(
        [user],
        "T1",
        "G2",
    )

    assert result == set()


def test_all_games_and_followed_team_does_not_duplicate():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        all_games_server_ping=True,
        followed_team_server_ping=True,
    )

    result = get_server_ping_user_ids(
        [user],
        "T1",
        "G2",
    )

    assert result == {1}


def test_dm_for_followed_team():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        dm_enabled=True,
        dm_reminder_minutes=30,
    )

    result = get_dm_user_ids(
        [user],
        "T1",
        "G2",
        30,
    )

    assert result == {1}


def test_dm_not_sent_at_wrong_time():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"T1"}),
        dm_enabled=True,
        dm_reminder_minutes=30,
    )

    result = get_dm_user_ids(
        [user],
        "T1",
        "G2",
        15,
    )

    assert result == set()


def test_dm_not_sent_for_unfollowed_match():
    user = UserReminderPreferences(
        user_id=1,
        followed_team_ids=frozenset({"GEN"}),
        dm_enabled=True,
        dm_reminder_minutes=30,
    )

    result = get_dm_user_ids(
        [user],
        "T1",
        "G2",
        30,
    )

    assert result == set()


def test_tracker_prevents_duplicate_server_reminder():
    tracker = ReminderTracker()

    key = ReminderKey(
        match_id="T1-G2",
        reminder_type=ReminderType.SERVER,
    )

    assert not tracker.has_sent(key)

    tracker.mark_sent(key)

    assert tracker.has_sent(key)


def test_tracker_distinguishes_dm_users():
    tracker = ReminderTracker()

    user_one = ReminderKey(
        match_id="T1-G2",
        reminder_type=ReminderType.DM,
        user_id=1,
    )

    user_two = ReminderKey(
        match_id="T1-G2",
        reminder_type=ReminderType.DM,
        user_id=2,
    )

    tracker.mark_sent(user_one)

    assert tracker.has_sent(user_one)

    assert not tracker.has_sent(user_two)


def test_naive_datetime_is_rejected():
    naive_start = datetime(
        2026,
        10,
        20,
        18,
        0,
    )

    with pytest.raises(ValueError):
        get_reminder_time(
            naive_start,
            5,
        )
