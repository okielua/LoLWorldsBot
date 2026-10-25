"""Durable end-of-series voting announcements and latest-result lookup."""

import asyncio
from contextlib import closing
from datetime import timedelta
import json
import logging
import re
import sqlite3

import discord

from models import MatchStatus
from services import polls
from services.match_times import format_tournament_date
from services.swiss import round_details
from services.presentation import message_payload
from services.reminder_engine import get_server_ping_user_ids
from services.server_settings import get_server_settings
from services.server_reminders import validate_reminder_channel
from services.spoilers import team_token
from services.team_history import HistoryPages
from services.user_settings import get_all_user_reminder_preferences, get_connection
from services.worlds_role import individual_subscribers
from services.prediction_form import add_prediction_form

logger = logging.getLogger(__name__)
SWISS_ROUND_MATCH_COUNTS = {1: 8, 2: 8, 3: 8, 4: 6, 5: 3}


def initialize():
    with closing(get_connection()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_scopes (
            guild_id INTEGER, tournament_id TEXT, PRIMARY KEY(guild_id, tournament_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS swiss_result_members (
            guild_id INTEGER, tournament_id TEXT, match_id TEXT, batch_key TEXT,
            PRIMARY KEY(guild_id, tournament_id, match_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_posts (
            guild_id INTEGER, tournament_id TEXT, match_id TEXT, channel_id INTEGER,
            message_id INTEGER, published_at TEXT, pages TEXT,
            PRIMARY KEY(guild_id, tournament_id, match_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS series_result_ping_batches (
            guild_id INTEGER, tournament_id TEXT, match_id TEXT, batch INTEGER,
            user_ids TEXT, sent INTEGER DEFAULT 0,
            PRIMARY KEY(guild_id, tournament_id, match_id, batch))""")


def latest_result(guild_id, tournament_id, *, match_id=None):
    initialize()
    with closing(get_connection()) as db:
        row = db.execute("""SELECT channel_id, message_id, pages FROM series_result_posts
            WHERE guild_id=? AND tournament_id=? AND message_id IS NOT NULL
              AND (? IS NULL OR match_id=? OR match_id IN (
                  SELECT batch_key FROM swiss_result_members WHERE guild_id=? AND tournament_id=? AND match_id=?))
            ORDER BY published_at DESC, rowid DESC LIMIT 1""",
            (guild_id, tournament_id, match_id, match_id, guild_id, tournament_id, match_id)).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def confirmed_result(match):
    return (match.status == MatchStatus.COMPLETED
            and "TBD" not in {match.team1_id, match.team2_id}
            and match.winner_id in {match.team1_id, match.team2_id}
            and match.team1_score is not None and match.team2_score is not None
            and max(match.team1_score, match.team2_score) == match.best_of // 2 + 1
            and min(match.team1_score, match.team2_score) >= 0
            and match.team1_score != match.team2_score
            and match.winner_id == (match.team1_id if match.team1_score > match.team2_score else match.team2_id))


def swiss_result_groups(data):
    groups = {}
    for match in sorted(data.matches, key=lambda m: (m.start_time, m.id)):
        if 'swiss' not in match.stage_id.lower() or match.status == MatchStatus.CANCELLED:
            continue
        round_number = re.match(r'Round (\d+)\b', match.round_name or '', re.I)
        # Without a confirmed round, wait for source metadata rather than announce
        # one match individually or mix different rounds.
        if round_number:
            key = f'swiss-round:{match.stage_id}:{int(round_number[1])}'
            groups.setdefault(key, []).append(match)
    return groups


def swiss_result_batches(data):
    groups = swiss_result_groups(data)
    batches = dict(groups)
    for key, matches in groups.items():
        if key.endswith((':2', ':3')) and len(matches) == 8:
            batches[key + ':after-4'] = matches[:4]
    return batches


def swiss_result_pages(matches, votes_by_match, *, test_mode=False, intermediate=False):
    from services.match_times import date_heading, TOURNAMENT_TIMEZONE
    from zoneinfo import ZoneInfo
    first = matches[0]
    round_number = re.match(r'Round (\d+)\b', first.round_name or '', re.I)[1]
    day = first.start_time.astimezone(ZoneInfo(TOURNAMENT_TIMEZONE)).date()
    heading = (f"# __{'TEST: ' if test_mode else ''}Worlds 2026 · Voting Results__\n"
               f"## __Swiss stage (Round {round_number})__\n"
               f"{date_heading(day, TOURNAMENT_TIMEZONE)}")
    if intermediate:
        heading += '\n\n**Intermediate results · First 4 of 8 matches**'
    sections, summaries = [], []
    for match in matches:
        votes = [(uid, team, name) for uid, team, name in votes_by_match.get(match.id, [])
                 if team in {match.team1_id, match.team2_id}]
        summary = (f"## {team_token(match.team1_id)} vs {team_token(match.team2_id)}\n"
                   f"**Final score: {match.team1_score}–{match.team2_score} · "
                   f"{len(votes)} vote{'s' if len(votes) != 1 else ''}**")
        details = summary
        for team_id in (match.team1_id, match.team2_id):
            names = sorted((' '.join((name or f'User {uid}').split())[:64]
                            for uid, team, name in votes if team == team_id), key=str.casefold)
            percent = len(names) / len(votes) if votes else 0
            line = f"**{team_token(team_id)} · {len(names)} ({percent:.0%})**"
            summary += '\n' + line
            details += '\n' + line
            icon = '✅' if team_id == match.winner_id else '❌'
            details += '\n' + ('\n'.join(f"{icon} @{discord.utils.escape_mentions(discord.utils.escape_markdown(name))}"
                                         for name in names) or 'No votes')
        sections.append(details)
        summaries.append(summary)
    footer = '\n\n-# • Use `/votehistory` for these voting results.'
    full = heading + '\n\n' + '\n\n'.join(sections) + footer
    # Budget rendered text too: tokens expand into custom emoji and team labels.
    from services.spoilers import render_text
    def fits(text):
        return max(len(text), len(render_text(text))) <= 3600
    if fits(full):
        return [full]
    overview = heading + '\n\n' + '\n\n'.join(summaries)
    overview += '\n\n-# Use Next to see every voter. All match results are shown above.'
    # Large voter lists stay in this same post, behind its page controls.
    pages = [overview]
    for match in matches:
        pages.extend(result_pages(match, votes_by_match.get(match.id, []), test_mode=test_mode))
    return pages


def completed_vote_result(manager):
    """Read real votes independently of whether an announcement was delivered."""
    with closing(get_connection()) as db:
        rows = db.execute("""SELECT match_id, team1_id, team2_id FROM match_polls
            WHERE guild_id=? AND tournament_id=? AND published_at IS NOT NULL AND closed=1
            ORDER BY COALESCE(result_observed_at, start_time) DESC, start_time DESC, match_id DESC""",
            (manager.guild_id, manager.store.data.tournament.id)).fetchall()
    for match_id, first, second in rows:
        match = manager.store.data.matches_by_id.get(match_id)
        if (match and confirmed_result(match)
                and {first, second} == {match.team1_id, match.team2_id}):
            return match, polls.get_poll_results(manager.guild_id, match_id)
    return None


def result_pages(match, votes, *, test_mode=False):
    """Explicit spoiler exception: completed-series results are fully visible."""
    pair = f"{team_token(match.team1_id)} vs {team_token(match.team2_id)}"
    heading = f"## {'TEST: ' if test_mode else ''}Series Voting Results\n\n## {pair}\n({format_tournament_date(match.start_time)})"
    heading += round_details(match)
    heading += f"\n\n**Final score:** {match.team1_score}–{match.team2_score}"
    valid = [(uid, team, name) for uid, team, name in votes if team in {match.team1_id, match.team2_id}]
    lines = []
    for team_id in ((match.team1_id, match.team2_id) if valid else ()):
        names = sorted(("@" + discord.utils.escape_mentions(discord.utils.escape_markdown(
            " ".join((name or f'User {uid}').split())[:64]))
            for uid, team, name in valid if team == team_id), key=str.casefold)
        count = len(names)
        label = f"{team_token(team_id)} · {count} vote{'s' if count != 1 else ''} ({count / len(valid):.0%})"
        icon = "✅" if team_id == match.winner_id else "❌"
        lines.append((label, [f"{icon} {name}" for name in names] or ["No votes"]))
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
        await interaction.followup.send(**message_payload("No completed series with a published vote is available in this server yet."),
                                        allowed_mentions=discord.AllowedMentions.none())
        return
    if manager.fresh():
        await asyncio.to_thread(polls.reconcile_polls, manager.guild_id, manager.store.data, manager.clock())
    completed = await asyncio.to_thread(completed_vote_result, manager)
    if completed:
        match, votes = completed
        pages = result_pages(match, votes, test_mode=manager.test_mode)
        pages = await add_prediction_form(pages, manager, [match], {match.id: votes})
        receipt = await asyncio.to_thread(latest_result, interaction.guild_id, manager.store.data.tournament.id, match_id=match.id)
        if receipt:
            channel_id, message_id, _ = receipt
            if 'Swiss stage (Round ' in receipt[2][0]:
                pages = receipt[2]
            link = f"\n\n[Open the results announcement](https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id})"
            pages = [page + link for page in pages]
        await interaction.followup.send(**message_payload(pages[0], controls=HistoryPages(pages)),
                                        allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.followup.send(**message_payload("No completed series with a published vote is available in this server yet."),
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
                for key, batch in swiss_result_batches(manager.store.data).items():
                    if all(confirmed_result(match) for match in batch):
                        db.execute("INSERT OR IGNORE INTO series_result_posts VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
                                   (*scope, key))
                db.execute("INSERT INTO series_result_scopes VALUES (?, ?)", scope)

    def _save(self, match_id, record):
        manager = self.manager
        channel, message, when, pages, remaining, *members = record
        scope = (manager.guild_id, manager.store.data.tournament.id, match_id)
        with closing(get_connection()) as db, db:
            db.execute("INSERT OR REPLACE INTO series_result_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (*scope, channel, message, when, json.dumps(pages)))
            if members:
                db.executemany("INSERT OR REPLACE INTO swiss_result_members VALUES (?, ?, ?, ?)",
                    [(manager.guild_id, manager.store.data.tournament.id, member, match_id) for member in members[0]])
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
            match = manager.store.data.matches_by_id.get(match_id)
            if match and 'swiss' in match.stage_id.lower():
                continue  # Do not resume leftover individual Swiss notification batches.
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
            batch = swiss_result_batches(manager.store.data).get(match_id)
            if batch:
                if not all(confirmed_result(item) for item in batch):
                    continue
                votes_by_match = {item.id: await asyncio.to_thread(polls.get_poll_results, manager.guild_id, item.id)
                                  for item in batch}
                pages = swiss_result_pages(batch, votes_by_match, test_mode=manager.test_mode,
                                           intermediate=match_id.endswith(':after-4'))
                pages = await add_prediction_form(pages, manager, batch, votes_by_match)
            else:
                if match is None or match.status != MatchStatus.COMPLETED:
                    continue
                votes = await asyncio.to_thread(polls.get_poll_results, manager.guild_id, match_id)
                pages = result_pages(match, votes, test_mode=manager.test_mode)
                pages = await add_prediction_form(pages, manager, [match], {match.id: votes})
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
                candidates = [(m.id, [m]) for m in manager.store.data.matches
                              if 'swiss' not in m.stage_id.lower()]
                candidates.extend(swiss_result_batches(manager.store.data).items())
                for receipt_key, batch in sorted(candidates, key=lambda item: item[1][0].start_time):
                    is_swiss = receipt_key.startswith('swiss-round:')
                    intermediate = receipt_key.endswith(':after-4')
                    # A 16-team Swiss round must be complete in the source too.
                    # Never fall back to a one-match post when fixtures are missing.
                    if is_swiss and not intermediate and len(batch) != SWISS_ROUND_MATCH_COUNTS.get(int(receipt_key.rsplit(':', 1)[1])):
                        continue
                    if intermediate and all(confirmed_result(m) for m in swiss_result_groups(manager.store.data)[receipt_key.rsplit(':', 1)[0]]):
                        continue  # If recovery happens after all eight finish, only send the final.
                    if (receipt_key in seen or receipt_key in self._pending
                            or not all(m.id in published and confirmed_result(m) for m in batch)
                            or ((retry_at := self._retry_at.get(receipt_key)) is not None
                                and manager.clock() < retry_at)):
                        continue
                    match = batch[0]
                    try:
                        channel = manager.client.get_channel(settings.reminder_channel_id) or await manager.client.fetch_channel(settings.reminder_channel_id)
                        validate_reminder_channel(channel, manager.guild_id)
                        votes_by_match = {m.id: await asyncio.to_thread(polls.get_poll_results, manager.guild_id, m.id)
                                          for m in batch}
                        pages = (swiss_result_pages(batch, votes_by_match, test_mode=manager.test_mode,
                                                    intermediate=intermediate) if is_swiss
                                 else result_pages(match, votes_by_match[match.id], test_mode=manager.test_mode))
                        pages = await add_prediction_form(pages, manager, batch, votes_by_match)
                        role = settings.worlds_role_id if not manager.test_mode else None
                        recipients = []
                        if not manager.test_mode:
                            users = await asyncio.to_thread(get_all_user_reminder_preferences)
                            recipients = await individual_subscribers(channel.guild,
                                set().union(*(get_server_ping_user_ids(users, m.team1_id, m.team2_id) for m in batch)), role)
                        capacity = min(90, max(0, (3800 - len(pages[0])) // 24))
                        first, remaining = recipients[:capacity], recipients[capacity:]
                        groups = [] if is_swiss else [remaining[i:i + 90] for i in range(0, len(remaining), 90)]
                        if is_swiss and remaining:
                            logger.warning("Swiss round %s: %s individual pings exceed the one-post limit; Worlds role still notified",
                                           receipt_key, len(remaining))
                        mentions = ([f"<@&{role}>"] if role else []) + [f"<@{uid}>" for uid in first]
                        text = pages[0] + ("\n\n" + " ".join(mentions) if mentions else "")
                        message = await channel.send(**message_payload(text, controls=HistoryPages(pages)),
                            allowed_mentions=discord.AllowedMentions(everyone=False, replied_user=False,
                                roles=[discord.Object(role)] if role else False,
                                users=[discord.Object(uid) for uid in first] if first else False))
                        record = (channel.id, message.id, manager.clock().isoformat(), pages, groups)
                        if is_swiss:
                            record += ([m.id for m in batch],)
                        self._pending[receipt_key] = record
                        # Commit after Discord acknowledgement without another await.
                        self._save(receipt_key, record)
                        del self._pending[receipt_key]
                        logger.info("Published voting results %s (%s matches) as message %s", receipt_key, len(batch), message.id)
                    except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
                        logger.exception("Could not announce completed voting results %s", receipt_key)
                        self._retry_at[receipt_key] = manager.clock() + timedelta(seconds=60)
                await self._remaining_pings()
            except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
                logger.exception("Could not refresh series voting results")
