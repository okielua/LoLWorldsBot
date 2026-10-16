import pytest

from services import user_settings
from services.user_settings import (
    get_all_user_reminder_preferences,
    get_followed_team_ids,
    get_user_timezone,
    set_followed_team_ids,
    set_user_timezone,
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "settings.db")
    user_settings.initialize_database()


def test_existing_follow_only_user_is_included_with_default_preferences():
    # Reproduce an existing DB row without invoking a preference setter.
    with user_settings.get_connection() as connection:
        connection.executemany(
            "INSERT INTO followed_teams (user_id, team_id) VALUES (?, ?)",
            [(123, "T1"), (123, "G2")],
        )
        assert connection.execute("SELECT * FROM user_settings").fetchall() == []

    preferences = get_all_user_reminder_preferences()

    assert len(preferences) == 1
    user = preferences[0]
    assert user.user_id == 123
    assert user.followed_team_ids == frozenset({"T1", "G2"})
    assert user.followed_team_server_ping is True
    assert user.all_games_server_ping is False
    assert user.dm_enabled is False
    assert user.dm_reminder_minutes == 0


def test_registered_zones_are_unique_current_and_ignore_invalid_existing_rows():
    from services.user_settings import get_registered_timezones
    assert get_registered_timezones() == []
    for user, zone in [(1, "Europe/Berlin"), (2, "Europe/Berlin"), (3, "Asia/Tokyo"), (4, "invalid"), (5, "")]:
        set_user_timezone(user, zone)
    assert get_registered_timezones() == ["Asia/Tokyo", "Europe/Berlin"]
    set_user_timezone(1, "Europe/London")
    set_user_timezone(2, "Europe/London")
    assert get_registered_timezones() == ["Asia/Tokyo", "Europe/London"]


def test_time_format_is_personal_validated_and_survives_restart():
    assert user_settings.get_user_time_format(123) == "24h"
    set_user_timezone(123, "Europe/Berlin")
    set_followed_team_ids(123, {"C9"})
    user_settings.set_user_time_format(123, "12h")
    user_settings.initialize_database()
    assert user_settings.get_user_time_format(123) == "12h"
    assert user_settings.get_user_time_format(456) == "24h"
    assert get_user_timezone(123) == "Europe/Berlin"
    assert get_followed_team_ids(123) == {"C9"}
    with pytest.raises(ValueError):
        user_settings.set_user_time_format(123, "invalid")
    assert user_settings.get_user_time_format(123) == "12h"
    user_settings.set_user_time_format(123, "24h")
    assert user_settings.get_user_time_format(123) == "24h"


def test_time_format_migration_preserves_existing_users(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "DATABASE_FILE", tmp_path / "legacy.db")
    with user_settings.get_connection() as connection:
        connection.execute("CREATE TABLE user_settings (user_id INTEGER PRIMARY KEY, timezone TEXT)")
        connection.execute("INSERT INTO user_settings VALUES (123, 'Europe/Berlin')")
    user_settings.initialize_database()
    assert user_settings.get_user_time_format(123) == "24h"
    assert get_user_timezone(123) == "Europe/Berlin"


def test_followed_teams_and_explicit_settings_are_combined_without_duplicates():
    set_followed_team_ids(123, {"T1", "G2"})
    user_settings.set_server_ping(123, False)
    user_settings.set_dm_notifications(123, True)
    user_settings.set_reminder_minutes(123, 30)
    user_settings.set_all_games_server_ping(123, True)

    preferences = get_all_user_reminder_preferences()

    assert len(preferences) == 1
    user = preferences[0]
    assert user.followed_team_ids == frozenset(get_followed_team_ids(123))
    assert user.followed_team_server_ping is False
    assert user.dm_enabled is True
    assert user.dm_reminder_minutes == 30
    assert user.all_games_server_ping is True


def test_user_timezone_can_be_saved():
    user_id = 999999001

    set_user_timezone(
        user_id,
        "Europe/Berlin",
    )

    assert get_user_timezone(user_id) == "Europe/Berlin"


def test_user_timezone_can_be_updated():
    user_id = 999999002

    set_user_timezone(
        user_id,
        "Europe/Berlin",
    )

    set_user_timezone(
        user_id,
        "Asia/Seoul",
    )

    assert get_user_timezone(user_id) == "Asia/Seoul"
