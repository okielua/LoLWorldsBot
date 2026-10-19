"""One Swiss Pick'ems closing reminder per server and tournament.

Riot's 2026 primer says Swiss picks lock before the first Swiss match:
https://lolesports.com/en-GB/news/worlds-2026-primer
The deadline therefore follows the first Swiss fixture's published start time.
"""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
import logging

import discord

from services.presentation import message_payload
from services.server_reminders import validate_reminder_channel
from services.server_settings import get_server_settings
from services.user_settings import get_connection

logger = logging.getLogger(__name__)


class PickemsReminder:
    def __init__(self, client, store, guild_id, *, clock=None, load_settings=None):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.load_settings = load_settings or get_server_settings
        self._lock = asyncio.Lock()
        self._pending = None

    def _receipt(self, tournament_id, message=None):
        with closing(get_connection()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS pickems_reminders (
                guild_id INTEGER, tournament_id TEXT, stage TEXT, message_id INTEGER,
                PRIMARY KEY(guild_id, tournament_id, stage))""")
            if message is not None:
                db.execute("INSERT OR IGNORE INTO pickems_reminders VALUES (?, ?, 'swiss', ?)",
                           (self.guild_id, tournament_id, message))
                return True
            return db.execute("SELECT 1 FROM pickems_reminders WHERE guild_id=? AND tournament_id=? AND stage='swiss'",
                              (self.guild_id, tournament_id)).fetchone() is not None

    async def tick(self):
        async with self._lock:
            # Persist acknowledged sends before doing anything that could send again.
            if self._pending:
                self._receipt(*self._pending)
                self._pending = None
            now = self.clock()
            if (self.store.last_error or self.store.fetched_at is None
                    or not timedelta(0) <= now - self.store.fetched_at <= timedelta(minutes=5)):
                return False
            data = self.store.data
            # This deadline rule is published for Worlds 2026, not future events.
            if data.tournament.id != 'worlds-2026':
                return False
            matches = [m for m in data.matches if 'swiss' in m.stage_id.lower()]
            if not matches:
                return False
            deadline = min(m.start_time for m in matches)
            if not deadline - timedelta(hours=8) <= now < deadline:
                return False
            if self._receipt(data.tournament.id):
                return False
            settings = self.load_settings(self.guild_id)
            if not (settings.public_reminders_enabled and settings.reminder_channel_id and settings.worlds_role_id):
                return False
            channel = self.client.get_channel(settings.reminder_channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(settings.reminder_channel_id)
            validate_reminder_channel(channel, self.guild_id)
            # Channel resolution can take time; don't announce an expired deadline.
            remaining = int((deadline - self.clock()).total_seconds() // 60)
            if remaining < 0 or self.clock() >= deadline:
                return False
            hours, minutes = divmod(remaining, 60)
            duration = (f"{hours} hour{'s' if hours != 1 else ''} and "
                        f"{minutes} minute{'s' if minutes != 1 else ''}")
            text = (f"# Swiss Pick’ems Close Soon!\n\n<@&{settings.worlds_role_id}>\n\n"
                    f"You have **{duration}** left to submit your Swiss Stage picks.\n"
                    f"Picks lock <t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>).\n\n"
                    "[Make your picks on LoL Esports](https://lolesports.com/en-GB/pickems)")
            message = await channel.send(**message_payload(text), allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=[discord.Object(settings.worlds_role_id)], replied_user=False))
            self._pending = (data.tournament.id, message.id)
            self._receipt(*self._pending)
            self._pending = None
            logger.info("Swiss Pick'ems closing reminder sent for %s", data.tournament.id)
            return True
