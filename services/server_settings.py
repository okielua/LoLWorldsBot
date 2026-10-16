"""Server-level settings, kept separate from personal /setup preferences."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from services.user_settings import get_connection


@dataclass(frozen=True)
class ServerSettings:
    guild_id: int
    reminder_channel_id: int | None = None
    public_reminders_enabled: bool = False
    pickems_url: str | None = None


def initialize_server_settings():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id INTEGER PRIMARY KEY,
                reminder_channel_id INTEGER,
                public_reminders_enabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(server_settings)")}
        if "pickems_url" not in columns:
            connection.execute("ALTER TABLE server_settings ADD COLUMN pickems_url TEXT")


def get_server_settings(guild_id: int) -> ServerSettings:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT reminder_channel_id, public_reminders_enabled, pickems_url FROM server_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return ServerSettings(guild_id, row[0], bool(row[1]), row[2]) if row else ServerSettings(guild_id)


def set_reminder_channel(guild_id: int, channel_id: int):
    if type(channel_id) is not int or channel_id <= 0:
        raise ValueError("Channel ID must be a positive integer.")
    with get_connection() as connection:
        # First channel selection enables reminders. Subsequent changes preserve the toggle.
        connection.execute("""
            INSERT INTO server_settings (guild_id, reminder_channel_id, public_reminders_enabled)
            VALUES (?, ?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET reminder_channel_id = excluded.reminder_channel_id
        """, (guild_id, channel_id))


def set_public_reminders_enabled(guild_id: int, enabled: bool):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT reminder_channel_id FROM server_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if enabled and (not row or row[0] is None):
            raise ValueError("Choose a reminder channel first.")
        connection.execute("""
            INSERT INTO server_settings (guild_id, public_reminders_enabled) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET public_reminders_enabled = excluded.public_reminders_enabled
        """, (guild_id, int(enabled)))


def set_pickems_url(guild_id, url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or any(char.isspace() or char in "<>" for char in url)):
        raise ValueError("Enter a valid HTTPS leaderboard link without spaces or credentials.")
    with get_connection() as connection:
        connection.execute("""
            INSERT INTO server_settings (guild_id, pickems_url) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET pickems_url = excluded.pickems_url
        """, (guild_id, url))
