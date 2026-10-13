import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_FILE = BASE_DIR / "data" / "worldsbot.db"


def get_connection():
    return sqlite3.connect(DATABASE_FILE)


def initialize_database():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT
            )
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
            [(user_id, team_id) for team_id in team_ids],
        )
