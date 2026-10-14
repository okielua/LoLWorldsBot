import sqlite3
from pathlib import Path

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
                reminder_minutes INTEGER NOT NULL DEFAULT 0
            )
            """)

        # Upgrade databases created before notification
        # settings existed.
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(user_settings)")
        }

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

        connection.execute("""
            CREATE TABLE IF NOT EXISTS followed_teams (
                user_id INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                PRIMARY KEY (user_id, team_id)
            )
            """)


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
