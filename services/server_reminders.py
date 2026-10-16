"""Deliver one public reminder per match; successful sends alone consume keys."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from math import ceil
import sqlite3

import discord

from models import MatchStatus
from services.delivery_records import DeliveryRecord, get_delivery_record, record_delivery
from services.reminder_engine import (
    ReminderKey, ReminderTracker, ReminderType, SERVER_RECOVERY_WINDOW_SECONDS,
    get_server_ping_user_ids, is_reminder_due,
)
from services.server_settings import get_server_settings
from services.prediction_display import results_embed
from services.user_settings import get_all_user_reminder_preferences


logger = logging.getLogger(__name__)


def validate_reminder_channel(channel, guild_id):
    if not isinstance(channel, discord.TextChannel) or channel.type != discord.ChannelType.text:
        raise ValueError("Choose a server text channel.")
    if channel.guild.id != guild_id:
        raise ValueError("Choose a channel in this server.")
    if channel.guild.me is None:
        raise ValueError("The bot's server membership is unavailable.")
    permissions = channel.permissions_for(channel.guild.me)
    if not permissions.view_channel or not permissions.send_messages:
        raise ValueError("Give the bot View Channel and Send Messages permission in that channel.")


class ServerReminderDelivery:
    def __init__(self, client, store, guild_id, *, clock=None, load_settings=None, load_users=None, message_prefix="", poll_manager=None):
        self.client = client
        self.store = store
        self.guild_id = guild_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.load_settings = load_settings or get_server_settings
        self.load_users = load_users or get_all_user_reminder_preferences
        self.message_prefix = message_prefix
        self.poll_manager = poll_manager
        self.tracker = ReminderTracker()
        self._lock = asyncio.Lock()
        self._pending_records: dict[str, DeliveryRecord] = {}
        self._retry_after: dict[str, datetime] = {}
        self._failures: dict[str, int] = {}

    def _schedule_retry(self, match_id, error):
        attempts = self._failures.get(match_id, 0) + 1
        self._failures[match_id] = attempts
        delay = min(15 * 2 ** min(attempts - 1, 2), 60)
        reason = "temporary delivery/database failure"
        if isinstance(error, discord.RateLimited):
            delay = max(delay, error.retry_after)
            reason = "Discord rate limit"
        elif isinstance(error, discord.HTTPException) and error.status in {400, 401, 403, 404}:
            delay = 60
            reason = "Discord channel/access failure; check /serversetup and bot permissions"
        elif isinstance(error, ValueError):
            delay = 60
            reason = "invalid reminder channel/message; check /serversetup and bot permissions"
        self._retry_after[match_id] = self.clock() + timedelta(seconds=delay)
        logger.warning("Public reminder for %s: %s; retry in %.0fs if still eligible: %s",
                       match_id, reason, delay, error)

    def _persist_pending_record(self, match_id):
        try:
            record_delivery(self._pending_records[match_id])
        except (sqlite3.Error, OSError, ValueError) as error:
            logger.error(
                "Reminder for %s was sent but its SQLite receipt could not be saved; "
                "keeping it in memory and retrying the write: %s", match_id, error,
            )
            return
        del self._pending_records[match_id]

    async def flush_pending_records(self):
        async with self._lock:
            for match_id in list(self._pending_records):
                self._persist_pending_record(match_id)

    def _eligible_match(self, match_id):
        now = self.clock()
        if self.store.last_error or self.store.fetched_at is None:
            return None
        if not timedelta(0) <= now - self.store.fetched_at <= timedelta(minutes=5):
            return None
        match = self.store.data.matches_by_id.get(match_id)
        if (match is None or match.status != MatchStatus.SCHEDULED
                or "TBD" in {match.team1_id, match.team2_id}
                or not is_reminder_due(match.start_time, now, 5, SERVER_RECOVERY_WINDOW_SECONDS)):
            return None
        return match

    async def deliver(self, match_id) -> bool:
        key = ReminderKey(match_id, ReminderType.SERVER)
        async with self._lock:
            if self.tracker.has_sent(key) or self._eligible_match(match_id) is None:
                return False
            if self.clock() < self._retry_after.get(match_id, self.clock()):
                return False
            try:
                existing = await asyncio.to_thread(get_delivery_record, self.guild_id, match_id)
                if existing is not None:
                    self.tracker.mark_sent(key)
                    return False
                settings = await asyncio.to_thread(self.load_settings, self.guild_id)
                if not settings.public_reminders_enabled or settings.reminder_channel_id is None:
                    return False
                channel = self.client.get_channel(settings.reminder_channel_id)
                if channel is None:
                    channel = await self.client.fetch_channel(settings.reminder_channel_id)
                validate_reminder_channel(channel, self.guild_id)
                prediction_results = ""
                if self.poll_manager:
                    prediction_results = await self.poll_manager.prepare_reminder(match_id)
                users = await asyncio.to_thread(self.load_users)
                # Recheck after I/O: an admin or refreshed source may have changed things.
                current = await asyncio.to_thread(self.load_settings, self.guild_id)
                if current != settings:
                    return False
                match = self._eligible_match(match_id)
                if match is None:
                    return False
                data = self.store.data
                team1 = discord.utils.escape_markdown(data.teams_by_id[match.team1_id].short_name)
                team2 = discord.utils.escape_markdown(data.teams_by_id[match.team2_id].short_name)
                recipients = sorted(get_server_ping_user_ids(users, match.team1_id, match.team2_id))
                remaining = (match.start_time - self.clock()).total_seconds()
                if remaining <= 0:
                    return False
                minutes = ceil(remaining / 60)
                timing = "less than a minute" if remaining < 60 else f"{minutes} minute{'s' if minutes != 1 else ''}"
                content = f"## **{team1}** vs **{team2}** starts in {timing}!"
                if self.message_prefix:
                    content = f"{self.message_prefix}\n\n{content}"
                if recipients:
                    content += "\n\n" + " ".join(f"<@{user_id}>" for user_id in recipients)
                presentation_options = {}
                message_content = content
                if prediction_results:
                    if not getattr(channel.permissions_for(channel.guild.me), "embed_links", True):
                        logger.warning("Embed Links permission is missing; sending plain prediction results.")
                        content += f"\n\n{prediction_results}"
                        message_content = content
                    else:
                        embed = results_embed(prediction_results)
                        embed.title = (
                            f"{data.teams_by_id[match.team1_id].short_name} vs "
                            f"{data.teams_by_id[match.team2_id].short_name}"
                        )
                        empty_note = f"\n{embed.description}" if embed.description else ""
                        embed.description = f"-# Starts in {timing}!\n\n__Voting Results__{empty_note}"
                        presentation_options["embed"] = embed
                        # Discord only sends mention notifications from message content.
                        outside = [self.message_prefix] if self.message_prefix else []
                        if recipients:
                            outside.append(" ".join(f"<@{user_id}>" for user_id in recipients))
                        message_content = "\n\n".join(outside) or None
                if len(recipients) > 100 or len(content) > 2000:
                    raise ValueError("Reminder exceeds Discord's single-message mention/content limits.")
                message = await channel.send(
                    message_content,
                    **presentation_options,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False,
                        users=[discord.Object(id=user_id) for user_id in recipients],
                        replied_user=False,
                    ),
                )
            except (discord.HTTPException, discord.RateLimited, sqlite3.Error, OSError, TimeoutError, ValueError) as error:
                self._schedule_retry(match_id, error)
                return False
            self.tracker.mark_sent(key)
            self._retry_after.pop(match_id, None)
            self._failures.pop(match_id, None)
            self._pending_records[match_id] = DeliveryRecord(
                self.guild_id, match_id, settings.reminder_channel_id, message.id, self.clock(),
            )
            # No await between Discord acknowledging the send and the SQLite commit:
            # ordinary task cancellation cannot interrupt this receipt write.
            self._persist_pending_record(match_id)
            logger.info("Sent public reminder for %s to channel %s", match_id, settings.reminder_channel_id)
            return True
