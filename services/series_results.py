"""Durable end-of-series voting announcements and latest-result lookup."""

import asyncio
from contextlib import closing
from datetime import timedelta
import json
import logging
import sqlite3

import discord

from models import MatchStatus
from services import polls
from services.match_times import format_tournament_date
from services.presentation import message_payload
from services.reminder_engine import get_server_ping_user_ids
from services.server_settings import get_server_settings
from services.server_reminders import validate_reminder_channel
from services.spoilers import score_token, team_token, result_token
from services.team_history import HistoryPages
from services.user_settings import get_all_user_reminder_preferences, get_connection
from services.worlds_role import individual_subscribers

logger = logging.getLogger(__name__)


def initialize():
    with closing(get_connection()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_scopes (
            guild_id INTEGER, tournament_id TEXT, PRIMARY KEY(guild_id, tournament_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_posts (
            guild_id INTEGER, tournament_id TEXT, match_id TEXT, channel_id INTEGER,
            message_id INTEGER, published_at TEXT, pages TEXT,
            PRIMARY KEY(guild_id, tournament_id, match_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_ping_batches (
            guild_id INTEGER, tournament_id TEXT, match_id TEXT, batch INTEGER,
            user_ids TEXT, sent INTEGER DEFAULT 0,
            PRIMARY KEY(guild_id, tournament_id, match_id, batch))""")


def latest_result(guild_id, tournament_id):
    initialize()
    with closing(get_connection()) as db:
        row = db.execute("""SELECT channel_id, message_id, pages FROM series_result_posts
            WHERE guild_id=? AND tournament_id=? AND message_id IS NOT NULL
            ORDER BY published_at DESC, rowid DESC LIMIT 1""", (guild_id, tournament_id)).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def result_pages(match, votes, *, test_mode=False):
    """Show stacked picks openly; protect the score and correctness icons."""
    pair = f"{team_token(match.team1_id)} vs {team_token(match.team2_id)}"
    heading = f"## {'TEST: ' if test_mode else ''}Series Voting Results\n\n## {pair}\n({format_tournament_date(match.start_time)})"
    score = (f"{match.team1_score}–{match.team2_score}" if test_mode else
             score_token(match) or f"||{match.team1_score}–{match.team2_score}||")
    heading += f"\n\n**Final score:** {'||' + score + '||' if test_mode else score}"
    valid = [(uid, team, name) for uid, team, name in votes if team in {match.team1_id, match.team2_id}]
    lines = []
    for team_id in ((match.team1_id, match.team2_id) if valid else ()):
        names = sorted(("@" + discord.utils.escape_mentions(discord.utils.escape_markdown(
            " ".join((name or f'User {uid}').split())[:64]))
            for uid, team, name in valid if team == team_id), key=str.casefold)
        count = len(names)
        label = f"{team_token(team_id)} · {count} vote{'s' if count != 1 else ''} ({count / len(valid):.0%})"
        icon = "✅" if team_id == match.winner_id else "❌"
        marker = f"||{icon}||" if test_mode else result_token(match, icon)
        lines.append((label, [f"{marker} {name}" for name in names] or ["No votes"]))
    total = f"**{len(valid)} vote{'s' if len(valid) != 1 else ''} cast**"
    pages, body = [], heading + "\n\n" + (total if valid else "No votes were cast for this series.")
    for label, names in lines:
        body += "\n\n### " + label
        for name in names:
            line = name
            if len(body) + len(line) > 3200:
                pages.append(body)
                body = heading + "\n\n### " + label + " (continued)"
            body += "\n" + line
    pages.append(body)
    footer = "\n\n-# • Use `/votehistory` for the latest series’ voting results.\n-# • Use `/votehistory @member` for a member’s prediction history."
    if test_mode:
        footer += "\n-# • Simulated results; no pings or changes to the real leaderboard."
    return [page + (f"\n\n-# Page {index + 1}/{len(pages)}" if len(pages) > 1 else "") + footer
            for index, page in enumerate(pages)]


async def show_latest_results(interaction):
    await interaction.response.defer()
    client = interaction.client
    manager = (getattr(client, 'prediction_test_manager', None)
               or getattr(client, 'last_prediction_test_manager', None)
               or client.voting_manager)
    if manager.guild_id != interaction.guild_id:
        await interaction.followup.send(**message_payload("No series voting results have been announced in this server yet."),
                                        allowed_mentions=discord.AllowedMentions.none())
        return
    latest = await asyncio.to_thread(latest_result, interaction.guild_id, manager.store.data.tournament.id)
    if latest:
        channel_id, message_id, pages = latest
        link = f"\n\n[Open the results announcement](https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id})"
        pages = [page + link for page in pages]
        await interaction.followup.send(**message_payload(pages[0], controls=HistoryPages(pages)),
                                        allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.followup.send(**message_payload("No series voting results have been announced in this server yet."),
                                        allowed_mentions=discord.AllowedMentions.none())


class SeriesResults:
    def __init__(self, manager):
        self.manager = manager
        self._lock = asyncio.Lock()
        self._pending = {}
        self._retry_at = {}
        self._sent_ping_batches = set()

    def _seed(self):
        initialize()
        manager = self.manager
        scope = (manager.guild_id, manager.store.data.tournament.id)
        with closing(get_connection()) as db, db:
            if not db.execute("SELECT 1 FROM series_result_scopes WHERE guild_id=? AND tournament_id=?", scope).fetchone():
                # First install must not announce every historical series.
                for match in manager.store.data.matches:
                    if match.status == MatchStatus.COMPLETED:
                        db.execute("INSERT OR IGNORE INTO series_result_posts VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
                                   (*scope, match.id))
                db.execute("INSERT INTO series_result_scopes VALUES (?, ?)", scope)

    def _save(self, match_id, record):
        manager = self.manager
        channel, message, when, pages, remaining = record
        scope = (manager.guild_id, manager.store.data.tournament.id, match_id)
        with closing(get_connection()) as db, db:
            db.execute("INSERT OR REPLACE INTO series_result_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (*scope, channel, message, when, json.dumps(pages)))
            for batch, users in enumerate(remaining):
                db.execute("INSERT OR IGNORE INTO series_result_ping_batches VALUES (?, ?, ?, ?, ?, 0)",
                           (*scope, batch, json.dumps(users)))

    async def _remaining_pings(self):
        manager = self.manager
        if manager.test_mode:
            return
        with closing(get_connection()) as db:
            rows = db.execute("""SELECT b.match_id, b.batch, b.user_ids, p.channel_id, p.message_id
                FROM series_result_ping_batches b JOIN series_result_posts p
                USING (guild_id, tournament_id, match_id)
                WHERE b.guild_id=? AND b.tournament_id=? AND b.sent=0 ORDER BY b.match_id, b.batch""",
                (manager.guild_id, manager.store.data.tournament.id)).fetchall()
        for match_id, batch, raw, channel_id, message_id in rows:
            users = json.loads(raw)
            key = (match_id, batch)
            if key not in self._sent_ping_batches:
                channel = manager.client.get_channel(channel_id) or await manager.client.fetch_channel(channel_id)
                text = (f"[Series voting results](https://discord.com/channels/{manager.guild_id}/{channel_id}/{message_id})\n\n"
                        + " ".join(f"<@{uid}>" for uid in users))
                await channel.send(**message_payload(text), allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(uid) for uid in users], replied_user=False))
                self._sent_ping_batches.add(key)
            with closing(get_connection()) as db, db:
                db.execute("UPDATE series_result_ping_batches SET sent=1 WHERE guild_id=? AND tournament_id=? AND match_id=? AND batch=?",
                           (manager.guild_id, manager.store.data.tournament.id, match_id, batch))
            self._sent_ping_batches.discard(key)

    async def _refresh_result_cards(self):
        """Silently refresh saved cards when the result layout changes."""
        manager = self.manager
        scope = (manager.guild_id, manager.store.data.tournament.id)
        with closing(get_connection()) as db:
            rows = db.execute("""SELECT match_id, channel_id, message_id, pages FROM series_result_posts
                WHERE guild_id=? AND tournament_id=? AND message_id IS NOT NULL""", scope).fetchall()
        for match_id, channel_id, message_id, saved in rows:
            match = manager.store.data.matches_by_id.get(match_id)
            if match is None or match.status != MatchStatus.COMPLETED:
                continue
            votes = await asyncio.to_thread(polls.get_poll_results, manager.guild_id, match_id)
            pages = result_pages(match, votes, test_mode=manager.test_mode)
            if pages == json.loads(saved):
                continue
            try:
                channel = manager.client.get_channel(channel_id) or await manager.client.fetch_channel(channel_id)
                await channel.get_partial_message(message_id).edit(
                    **message_payload(pages[0], controls=HistoryPages(pages)), allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                # Keep the retrievable result even if its original post was deleted.
                pass
            except discord.HTTPException:
                logger.warning("Could not refresh series result card %s; will retry", message_id, exc_info=True)
                continue
            with closing(get_connection()) as db, db:
                db.execute("UPDATE series_result_posts SET pages=? WHERE guild_id=? AND tournament_id=? AND match_id=?",
                           (json.dumps(pages), *scope, match_id))

    async def tick(self):
        manager = self.manager
        if not manager.fresh():
            return
        async with self._lock:
            try:
                self._seed()
                for match_id, record in list(self._pending.items()):
                    self._save(match_id, record)
                    del self._pending[match_id]
                await self._refresh_result_cards()
                with closing(get_connection()) as db:
                    seen = {row[0] for row in db.execute("SELECT match_id FROM series_result_posts WHERE guild_id=? AND tournament_id=?",
                            (manager.guild_id, manager.store.data.tournament.id))}
                published = await asyncio.to_thread(polls.get_published_match_ids, manager.guild_id, manager.store.data.tournament.id)
                settings = await asyncio.to_thread(get_server_settings, manager.guild_id)
                if not settings.public_reminders_enabled or settings.reminder_channel_id is None:
                    return
                for match in sorted(manager.store.data.matches, key=lambda item: item.start_time):
                    if (match.id in seen or match.id in self._pending or match.id not in published
                            or match.status != MatchStatus.COMPLETED or match.winner_id not in {match.team1_id, match.team2_id}
                            or match.team1_score is None or match.team2_score is None
                            or max(match.team1_score, match.team2_score) != match.best_of // 2 + 1
                            or min(match.team1_score, match.team2_score) < 0
                            or match.team1_score == match.team2_score
                            or match.winner_id != (match.team1_id if match.team1_score > match.team2_score else match.team2_id)
                            or manager.clock() < self._retry_at.get(match.id, manager.clock())):
                        continue
                    try:
                        channel = manager.client.get_channel(settings.reminder_channel_id) or await manager.client.fetch_channel(settings.reminder_channel_id)
                        validate_reminder_channel(channel, manager.guild_id)
                        votes = await asyncio.to_thread(polls.get_poll_results, manager.guild_id, match.id)
                        pages = result_pages(match, votes, test_mode=manager.test_mode)
                        role = settings.worlds_role_id if not manager.test_mode else None
                        recipients = []
                        if not manager.test_mode:
                            users = await asyncio.to_thread(get_all_user_reminder_preferences)
                            recipients = await individual_subscribers(channel.guild,
                                get_server_ping_user_ids(users, match.team1_id, match.team2_id), role)
                        capacity = min(90, max(0, (3800 - len(pages[0])) // 24))
                        first, remaining = recipients[:capacity], recipients[capacity:]
                        groups = [remaining[i:i + 90] for i in range(0, len(remaining), 90)]
                        mentions = ([f"<@&{role}>"] if role else []) + [f"<@{uid}>" for uid in first]
                        text = pages[0] + ("\n\n" + " ".join(mentions) if mentions else "")
                        message = await channel.send(**message_payload(text, controls=HistoryPages(pages)),
                            allowed_mentions=discord.AllowedMentions(everyone=False, replied_user=False,
                                roles=[discord.Object(role)] if role else False,
                                users=[discord.Object(uid) for uid in first] if first else False))
                        record = (channel.id, message.id, manager.clock().isoformat(), pages, groups)
                        self._pending[match.id] = record
                        # Commit after Discord acknowledgement without another await.
                        self._save(match.id, record)
                        del self._pending[match.id]
                    except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
                        logger.exception("Could not announce completed series %s", match.id)
                        self._retry_at[match.id] = manager.clock() + timedelta(seconds=60)
                await self._remaining_pings()
            except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
                logger.exception("Could not refresh series voting results")
