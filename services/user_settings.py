import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from services.reminder_engine import UserReminderPreferences

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_FILE = BASE_DIR / "data" / "worldsbot.db"


def get_connection():
    return sqlite3.connect(DATABASE_FILE)


def initialize_database():
    DATABASE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT,
                dm_notifications INTEGER NOT NULL DEFAULT 0,
                server_ping INTEGER NOT NULL DEFAULT 1,
                reminder_minutes INTEGER NOT NULL DEFAULT 0,
                all_games_server_ping INTEGER NOT NULL DEFAULT 0
            )
            """)

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(user_settings)")
        }

        if "time_format" not in columns:
            connection.execute("ALTER TABLE user_settings ADD COLUMN time_format TEXT NOT NULL DEFAULT '24h'")

        if "dm_notifications" not in columns:
            connection.execute("""
                ALTER TABLE user_settings
                ADD COLUMN dm_notifications
                INTEGER NOT NULL DEFAULT 0
                """)

        if "server_ping" not in columns:
            connection.execute("""
                ALTER TABLE user_settings
                ADD COLUMN server_ping
                INTEGER NOT NULL DEFAULT 1
                """)

        if "reminder_minutes" not in columns:
            connection.execute("""
                ALTER TABLE user_settings
                ADD COLUMN reminder_minutes
                INTEGER NOT NULL DEFAULT 0
                """)

        if "all_games_server_ping" not in columns:
            connection.execute("""
                ALTER TABLE user_settings
                ADD COLUMN all_games_server_ping
                INTEGER NOT NULL DEFAULT 0
                """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS followed_teams (
                user_id INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                PRIMARY KEY (user_id, team_id)
            )
            """)


def get_user_time_format(user_id: int) -> str:
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT time_format FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return "12h" if row and row[0] == "12h" else "24h"


def set_user_time_format(user_id: int, time_format: str) -> None:
    if time_format not in {"12h", "24h"}:
        raise ValueError("Choose either 12-hour or 24-hour time.")
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            INSERT INTO user_settings (user_id, time_format) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET time_format = excluded.time_format
        """, (user_id, time_format))


def get_user_timezone(
    user_id: int,
) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT timezone
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        return None

    return row[0]


def set_user_timezone(
    user_id: int,
    timezone: str,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (
                user_id,
                timezone
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                timezone = excluded.timezone
            """,
            (
                user_id,
                timezone,
            ),
        )


def get_registered_timezones() -> list[str]:
    """Unique valid setup zones, including preferences saved before this feature."""
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT DISTINCT timezone FROM user_settings WHERE timezone IS NOT NULL ORDER BY timezone"
        ).fetchall()
    zones = []
    for (name,) in rows:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        zones.append(name)
    return zones


def get_followed_team_ids(
    user_id: int,
) -> set[str]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT team_id
            FROM followed_teams
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

    return {row[0] for row in rows}


def set_followed_team_ids(
    user_id: int,
    team_ids: set[str],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            DELETE FROM followed_teams
            WHERE user_id = ?
            """,
            (user_id,),
        )

        connection.executemany(
            """
            INSERT INTO followed_teams (
                user_id,
                team_id
            )
            VALUES (?, ?)
            """,
            [
                (
                    user_id,
                    team_id,
                )
                for team_id in team_ids
            ],
        )


def get_notification_settings(
    user_id: int,
) -> tuple[bool, bool, int]:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                dm_notifications,
                server_ping,
                reminder_minutes
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        return (
            False,
            True,
            0,
        )

    return (
        bool(row[0]),
        bool(row[1]),
        int(row[2]),
    )


def get_all_games_server_ping(
    user_id: int,
) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT all_games_server_ping
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        return False

    return bool(row[0])


def set_all_games_server_ping(
    user_id: int,
    enabled: bool,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (
                user_id,
                all_games_server_ping
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                all_games_server_ping =
                    excluded.all_games_server_ping
            """,
            (
                user_id,
                int(enabled),
            ),
        )


def set_dm_notifications(
    user_id: int,
    enabled: bool,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (
                user_id,
                dm_notifications
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                dm_notifications = excluded.dm_notifications
            """,
            (
                user_id,
                int(enabled),
            ),
        )


def set_server_ping(
    user_id: int,
    enabled: bool,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (
                user_id,
                server_ping
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                server_ping = excluded.server_ping
            """,
            (
                user_id,
                int(enabled),
            ),
        )


def set_reminder_minutes(
    user_id: int,
    minutes: int,
) -> None:
    allowed_minutes = {
        0,
        15,
        30,
        60,
    }

    if minutes not in allowed_minutes:
        raise ValueError("Reminder must be 0, 15, 30, or 60 minutes.")

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (
                user_id,
                reminder_minutes
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                reminder_minutes = excluded.reminder_minutes
            """,
            (
                user_id,
                minutes,
            ),
        )


# Load every user's notification preferences and followed teams
# into the format used by the reminder engine.
def get_all_user_reminder_preferences() -> list[UserReminderPreferences]:
    with get_connection() as connection:
        user_rows = connection.execute("""
            SELECT
                users.user_id,
                COALESCE(settings.dm_notifications, 0),
                COALESCE(settings.server_ping, 1),
                COALESCE(settings.reminder_minutes, 0),
                COALESCE(settings.all_games_server_ping, 0)
            FROM (
                SELECT user_id FROM user_settings
                UNION
                SELECT user_id FROM followed_teams
            ) AS users
            LEFT JOIN user_settings AS settings
                ON settings.user_id = users.user_id
            """).fetchall()

        followed_rows = connection.execute("""
            SELECT
                user_id,
                team_id
            FROM followed_teams
            """).fetchall()

    followed_by_user: dict[int, set[str]] = {}

    for user_id, team_id in followed_rows:
        followed_by_user.setdefault(
            user_id,
            set(),
        ).add(team_id)

    preferences = []

    for (
        user_id,
        dm_notifications,
        server_ping,
        reminder_minutes,
        all_games_server_ping,
    ) in user_rows:
        preferences.append(
            UserReminderPreferences(
                user_id=user_id,
                followed_team_ids=frozenset(
                    followed_by_user.get(
                        user_id,
                        set(),
                    )
                ),
                all_games_server_ping=bool(all_games_server_ping),
                followed_team_server_ping=bool(server_ping),
                dm_enabled=bool(dm_notifications),
                dm_reminder_minutes=int(reminder_minutes),
            )
        )

    return preferences
