"""Successful public Discord sends, persisted in the existing SQLite database."""

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

from services.user_settings import get_connection


@dataclass(frozen=True)
class DeliveryRecord:
    guild_id: int
    match_id: str
    channel_id: int
    message_id: int
    sent_at: datetime


def initialize_delivery_records():
    with closing(get_connection()) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS server_reminder_deliveries (
                guild_id INTEGER NOT NULL,
                match_id TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, match_id)
            )
        """)


def get_delivery_record(guild_id: int, match_id: str) -> DeliveryRecord | None:
    with closing(get_connection()) as connection:
        row = connection.execute("""
            SELECT channel_id, message_id, sent_at
            FROM server_reminder_deliveries WHERE guild_id = ? AND match_id = ?
        """, (guild_id, match_id)).fetchone()
    if row is None:
        return None
    return DeliveryRecord(guild_id, match_id, row[0], row[1], datetime.fromisoformat(row[2]))


def record_delivery(record: DeliveryRecord):
    if record.sent_at.tzinfo is None:
        raise ValueError("Delivery timestamp must be timezone-aware.")
    if any(type(value) is not int or value <= 0 for value in
           (record.guild_id, record.channel_id, record.message_id)):
        raise ValueError("Discord IDs must be positive integers.")
    if not isinstance(record.match_id, str) or not record.match_id:
        raise ValueError("Match ID must be a nonempty string.")
    with closing(get_connection()) as connection, connection:
        # Channel changes and reschedules do not grant a second reminder for the same match.
        connection.execute("""
            INSERT INTO server_reminder_deliveries
                (guild_id, match_id, channel_id, message_id, sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, match_id) DO NOTHING
        """, (record.guild_id, record.match_id, record.channel_id, record.message_id,
              record.sent_at.astimezone(UTC).isoformat()))
