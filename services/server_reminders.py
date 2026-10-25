"""Deliver one public reminder per match; successful sends alone consume keys."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import sqlite3

import discord
from services.presentation import message_payload

from services.delivery_records import DeliveryRecord, get_delivery_record, record_delivery
from services.reminder_engine import (
    ReminderKey, ReminderTracker, ReminderType, SERVER_RECOVERY_WINDOW_SECONDS,
    get_server_ping_user_ids, public_reminder_due,
)
from services.server_settings import get_server_settings
from services.prediction_display import results_embed, format_prediction_results, VOTING_FOOTER
from services.user_settings import get_all_user_reminder_preferences
from services.voting_rules import swiss_batch, reminder_receipt_key
from services.status_labels import upcoming_line


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
    def __init__(self, client, store, guild_id, *, clock=None, load_settings=None, load_users=None, message_prefix="", poll_manager=None, ping_role=True):
        self.client = client
        self.store = store
        self.guild_id = guild_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.load_settings = load_settings or get_server_settings
        self.load_users = load_users or get_all_user_reminder_preferences
        self.message_prefix = message_prefix
        self.poll_manager = poll_manager
        self.ping_role = ping_role
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
        if match and swiss_batch(self.store.data, match)[0].id != match_id:
            return None  # One reminder for the entire Swiss Bo1 matchday.
        if (match is None
                or "TBD" in {match.team1_id, match.team2_id}
                or not public_reminder_due(match, now, SERVER_RECOVERY_WINDOW_SECONDS)):
            return None
        return match

    async def deliver(self, match_id) -> bool:
        key = ReminderKey(match_id, ReminderType.SERVER)
        async with self._lock:
            if self.tracker.has_sent(key) or self._eligible_match(match_id) is None:
                return False
            retry_at = self._retry_after.get(match_id)
            if retry_at is not None and self.clock() < retry_at:
                return False
            logger.info("Preparing public reminder for %s", match_id)
            try:
                receipt_key = reminder_receipt_key(self.store.data, self.store.data.matches_by_id[match_id])
                existing = await asyncio.to_thread(get_delivery_record, self.guild_id, receipt_key)
                if existing is not None:
                    logger.info("Public reminder for %s already recorded as message %s", match_id, existing.message_id)
                    self.tracker.mark_sent(key)
                    return False
                settings = await asyncio.to_thread(self.load_settings, self.guild_id)
                if not settings.public_reminders_enabled or settings.reminder_channel_id is None:
                    logger.warning("Public reminder for %s skipped: reminders disabled or channel unset", match_id)
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
                    logger.warning("Public reminder for %s deferred: settings changed during preparation", match_id)
                    return False
                match = self._eligible_match(match_id)
                if match is None:
                    logger.warning("Public reminder for %s skipped: no longer eligible after preparation", match_id)
                    return False
                data = self.store.data
                batch = swiss_batch(data, match)
                team1 = discord.utils.escape_markdown(data.teams_by_id[match.team1_id].short_name)
                team2 = discord.utils.escape_markdown(data.teams_by_id[match.team2_id].short_name)
                if not prediction_results:
                    prediction_results = format_prediction_results(match, [])
                recipients = sorted(get_server_ping_user_ids(users, match.team1_id, match.team2_id))
                if len(batch) > 1:
                    recipients = sorted(set().union(*(get_server_ping_user_ids(users, m.team1_id, m.team2_id) for m in batch)))
                remaining = (match.start_time - self.clock()).total_seconds()
                if remaining <= 0:
                    return False
                from services.spoilers import current_policy, matchup
                pairing = matchup(match) if current_policy() else f"{team1} vs {team2}"
                stage = data.stages_by_id.get(match.stage_id)
                stage_name = discord.utils.escape_markdown(stage.name if stage else "Stage to Be Confirmed")
                round_label = f" · {match.round_name}" if match.round_name and "swiss" in match.stage_id.lower() else ""
                content = (f"## {pairing}\n**{stage_name}{round_label} · Bo{match.best_of}**\n\n"
                           + upcoming_line(match, self.clock()))
                if self.message_prefix:
                    content += f"\n\n{self.message_prefix}"
                results = results_embed(prediction_results, include_help=False)
                content += "\n\n### __Voting Results__"
                if results.description:
                    content += f"\n{results.description}"
                else:
                    for field in results.fields:
                        content += f"\n\n### {field.name}\n{field.value.rstrip(chr(0x200b)).strip()}"
                if len(batch) > 1:
                    pairings = "\n".join(f"### {matchup(m)}" for m in batch)
                    batch_label = f"{stage_name}{round_label} · Bo{match.best_of}"
                    if len({m.best_of for m in batch}) > 1:
                        batch_label = f"{stage_name} · Round 3"
                        pairings = '\n'.join(f"### {matchup(m)}\n**{m.round_name} · Bo{m.best_of}**" for m in batch)
                    content = (f"## Swiss Matchday\n**{batch_label}**\n\n"
                               + upcoming_line(match, self.clock()) + '\n\n'
                               f"Voting is now closed for **all {len(batch)} matches**.\n\n{pairings}")
                    if self.message_prefix:
                        content += "\n\n" + self.message_prefix
                role_id = settings.worlds_role_id
                if role_id and recipients:
                    # The role mention already notifies role subscribers.
                    individual_recipients = []
                    for user_id in recipients:
                        try:
                            member = channel.guild.get_member(user_id) or await channel.guild.fetch_member(user_id)
                        except discord.NotFound:
                            continue
                        if not any(role.id == role_id for role in member.roles):
                            individual_recipients.append(user_id)
                    recipients = individual_recipients
                if role_id or recipients:
                    content += "\n\n### __Subscribers:__"
                    if role_id:
                        content += f"\n<@&{role_id}>"
                    if recipients:
                        content += "\n\n" + " ".join(f"<@{user_id}>" for user_id in recipients)
                content += "\n\n" + VOTING_FOOTER
                payload = message_payload(content)
                text_size = sum(len(item.content) for item in payload["view"].walk_children()
                                if isinstance(item, discord.ui.TextDisplay))
                if len(recipients) > 100 or text_size > 4000:
                    raise ValueError("Reminder exceeds Discord's single-message mention/content limits.")
                if self._eligible_match(match_id) is None:
                    logger.warning("Public reminder for %s skipped: eligibility expired before send", match_id)
                    return False
                logger.info("Sending public reminder for %s to channel %s", match_id, channel.id)
                message = await channel.send(
                    **payload,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=[discord.Object(id=role_id)] if role_id and self.ping_role else False,
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
                self.guild_id, receipt_key, settings.reminder_channel_id, message.id, self.clock(),
            )
            # No await between Discord acknowledging the send and the SQLite commit:
            # ordinary task cancellation cannot interrupt this receipt write.
            self._persist_pending_record(match_id)
            logger.info("Sent public reminder for %s to channel %s", match_id, settings.reminder_channel_id)
            return True
