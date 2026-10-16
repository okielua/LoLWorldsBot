"""One private-prediction post per Worlds matchday, with five-minute cutoffs."""

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import sqlite3
from zoneinfo import ZoneInfo

import discord
from services.presentation import embed_card_payload, message_payload

from models import MatchStatus
from services import polls
from services.match_times import TOURNAMENT_TIMEZONE, private_match_details, registered_match_times
from services.user_settings import get_registered_timezones, get_user_timezone
from services.prediction_display import PREDICTION_BLUE, VOTE_CONFIRMATION_HELP, format_prediction_results
from services.server_reminders import validate_reminder_channel
from services.server_settings import get_server_settings
from services.prediction_stats import get_previous_day_mvp, get_voter_display_names
from services.status_labels import VOTING_CLOSED, VOTING_OPEN

logger = logging.getLogger(__name__)
MATCHDAY_ZONE = ZoneInfo(TOURNAMENT_TIMEZONE)


class VoteButton(discord.ui.Button):
    def __init__(self, manager, poll, index):
        self.manager, self.poll = manager, poll
        self.team_id = poll.team1_id if index == 1 else poll.team2_id
        super().__init__(
            label=self.team_id, style=discord.ButtonStyle.primary, disabled=poll.closed,
            custom_id=f"worldsvote:{poll.guild_id}:{poll.match_id}:{index}",
        )

    async def callback(self, interaction):
        if interaction.guild_id != self.poll.guild_id:
            await interaction.response.send_message(**message_payload("This poll belongs to another server."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        saved = False
        try:
            await self.manager.vote(
                self.poll.match_id, interaction.user.id, self.team_id, interaction.user.display_name,
                expected_pair=(self.poll.team1_id, self.poll.team2_id),
            )
            match = self.manager.store.data.matches_by_id.get(self.poll.match_id, self.poll)
            team1 = discord.utils.escape_markdown(match.team1_id)
            team2 = discord.utils.escape_markdown(match.team2_id)
            team = discord.utils.escape_markdown(self.team_id)
            timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
            date, _ = private_match_details(match.start_time, timezone_name)
            text = (
                f"Your prediction for **{team1}** vs **{team2}** on {date}:\n"
                f"## {team}"
            )
            saved = True
        except ValueError as error:
            text = str(error)
        except sqlite3.Error:
            logger.exception("Could not save prediction")
            text = "Couldn't save your prediction. Please try again."
        embed = discord.Embed(description=text, colour=PREDICTION_BLUE)
        if saved:
            embed.add_field(name="\u200b", value=VOTE_CONFIRMATION_HELP, inline=False)
        await interaction.followup.send(**embed_card_payload(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PersonalVoteView(discord.ui.View):
    def __init__(self, manager, poll, user_id, selected_team=None):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.add_item(VoteButton(manager, poll, 1))
        self.add_item(VoteButton(manager, poll, 2))
        for button in self.children:
            if button.team_id == selected_team:
                button.label = f"{button.team_id} ✓"
                button.style = discord.ButtonStyle.success

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/changevote` to make or change your prediction."), ephemeral=True)
            return False
        return True


class DailyPredictionsView(discord.ui.LayoutView):
    def __init__(self, manager, day, matches, poll_by_id, recap=""):
        super().__init__(timeout=None)
        # Worlds has at most eight matches per day in the verified schedule.
        # Each match consumes four V2 components, plus the card and two text blocks.
        if len(matches) > 9:
            raise ValueError("This matchday exceeds Discord's single-message component limit.")
        date = datetime.fromisoformat(day)
        timezones = get_registered_timezones()
        card = discord.ui.Container(accent_colour=discord.Colour(PREDICTION_BLUE))
        self.add_item(card)
        card.add_item(discord.ui.TextDisplay(
            f"# __{'TEST: ' if manager.test_mode else ''}Worlds Predictions__\n## __{date.strftime('%A')}, {date.day} {date.strftime('%B %Y')}__\n"
            + recap
        ))
        for match_index, match in enumerate(matches):
            poll = poll_by_id.get(match.id)
            if poll is None:
                poll = polls.Poll(
                    manager.guild_id, match.id, match.tournament_id, match.team1_id, match.team2_id,
                    match.start_time, True, None,
                )
            status = (
                "Teams to be confirmed." if "TBD" in {match.team1_id, match.team2_id}
                else VOTING_CLOSED if poll.closed else f"{VOTING_OPEN} · Who wins?"
            )
            stage = manager.store.data.stages_by_id.get(match.stage_id)
            stage_name = stage.name if stage else "Stage to be confirmed"
            if stage_name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
                stage_name = "Play-ins"
            details = [stage_name]
            def normalize(label):
                return label.lower().replace("-", "").replace(" ", "").rstrip("s")

            if match.round_name and normalize(match.round_name) != normalize(stage_name):
                details.append(match.round_name)
            details.append(f"Bo{match.best_of}")
            stage_label = discord.utils.escape_markdown(" · ".join(details))
            # An invisible line preserves whitespace without adding components:
            # eight-match days must still fit Discord's 40-component limit.
            gap = "\u200b\n\n" if match_index else ""
            times = "\n".join(f"-# {line}" for line in registered_match_times(match.start_time, timezones).splitlines())
            card.add_item(discord.ui.TextDisplay(
                f"{gap}{stage_label}\n## {match.team1_id} vs {match.team2_id}\n"
                f"{times}\n{status}"
            ))
            card.add_item(discord.ui.ActionRow(VoteButton(manager, poll, 1), VoteButton(manager, poll, 2)))
        card.add_item(discord.ui.TextDisplay(
            VOTE_CONFIRMATION_HELP
        ))


class VotingManager:
    def __init__(self, client, store, guild_id, *, clock=None, test_mode=False):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.test_mode = test_mode
        self._lock = asyncio.Lock()
        self._posted = {}
        self._retry_at = {}
        self._registered = set()
        self._publication_queue = {}
        self._checked_at = {}

    def fresh(self):
        return (
            self.store.fetched_at is not None and not self.store.last_error
            and timedelta(0) <= self.clock() - self.store.fetched_at <= timedelta(minutes=5)
        )

    def match_days(self):
        days = {}
        for match in sorted(self.store.data.matches, key=lambda item: item.start_time):
            day = match.start_time.astimezone(MATCHDAY_ZONE).date().isoformat()
            days.setdefault(day, []).append(match)
        return days

    async def vote(self, match_id, user_id, team_id, display_name=None, expected_pair=None):
        match = self.store.data.matches_by_id.get(match_id)
        if not self.fresh():
            raise ValueError("Live data is temporarily unavailable; please try again shortly.")
        if (
            match is None or match.status != MatchStatus.SCHEDULED
            or "TBD" in {match.team1_id, match.team2_id}
            or self.clock() >= match.start_time - timedelta(minutes=5)
        ):
            raise ValueError("Predictions have closed for this match.")
        if team_id not in {match.team1_id, match.team2_id}:
            raise ValueError("This matchup changed. Please use the current prediction buttons.")
        if expected_pair is not None and set(expected_pair) != {match.team1_id, match.team2_id}:
            raise ValueError("This matchup changed. Please use the current prediction buttons.")
        await asyncio.to_thread(polls.reconcile_polls, self.guild_id, self.store.data, self.clock())
        current = self.store.data.matches_by_id.get(match_id)
        if not self.fresh() or current is None or current.status != MatchStatus.SCHEDULED or self.clock() >= current.start_time - timedelta(minutes=5):
            raise ValueError("Predictions have closed or live data is unavailable. Please try again shortly.")
        if {current.team1_id, current.team2_id} != {match.team1_id, match.team2_id}:
            raise ValueError("This matchup changed. Please use the current prediction buttons.")
        return await asyncio.to_thread(polls.cast_vote, self.guild_id, match_id, user_id, team_id, self.clock(), display_name,
                                       (match.team1_id, match.team2_id))

    async def personal_vote_target(self, user_id):
        if not self.fresh():
            raise ValueError("Live data is temporarily unavailable; please try again shortly.")
        data = self.store.data
        await asyncio.to_thread(polls.reconcile_polls, self.guild_id, data, self.clock())
        available = await asyncio.to_thread(polls.get_polls, self.guild_id, data.tournament.id)
        published = await asyncio.to_thread(polls.get_published_match_ids, self.guild_id, data.tournament.id)
        latest = await asyncio.to_thread(polls.get_latest_vote_match_id, self.guild_id, data.tournament.id, user_id)
        candidates = []
        for poll in available:
            match = data.matches_by_id.get(poll.match_id)
            if (match and poll.match_id in published and not poll.closed and match.status == MatchStatus.SCHEDULED
                    and "TBD" not in {match.team1_id, match.team2_id}
                    and self.clock() < match.start_time - timedelta(minutes=5)):
                candidates.append(poll)
        return min(candidates, key=lambda poll: (poll.match_id != latest, poll.start_time), default=None)

    def _acknowledge_opportunities(self, matches, poll_by_id):
        at = self.clock()
        for match in matches:
            poll = poll_by_id.get(match.id)
            if poll and not poll.closed and at < match.start_time - timedelta(minutes=5):
                key = (match.id, match.team1_id, match.team2_id)
                self._publication_queue.setdefault(key, at)

    async def _flush_opportunities(self):
        for key, at in list(self._publication_queue.items()):
            try:
                await asyncio.to_thread(polls.record_poll_opportunity, self.guild_id, *key, at)
            except (sqlite3.Error, OSError):
                logger.exception("Could not persist acknowledged prediction opportunity")
            else:
                self._publication_queue.pop(key, None)

    async def flush_pending_records(self):
        async with self._lock:
            await self._flush_opportunities()
            for day, receipt in self._posted.items():
                try:
                    await asyncio.to_thread(polls.save_daily_post, self.guild_id, self.store.data.tournament.id, day, *receipt)
                except (sqlite3.Error, OSError):
                    logger.exception("Could not flush daily prediction receipt for %s", day)

    async def _sync_day(self, day, matches):
        async with self._lock:
            if self.clock() < self._retry_at.get(day, self.clock()):
                return None
            tournament_id = self.store.data.tournament.id
            receipt = await asyncio.to_thread(polls.get_daily_post, self.guild_id, tournament_id, day)
            if day in self._posted and self._posted[day] != receipt:
                receipt = self._posted[day]
                await asyncio.to_thread(polls.save_daily_post, self.guild_id, tournament_id, day, *receipt)
            matches = [m for m in matches if "TBD" not in {m.team1_id, m.team2_id}]
            if receipt is None:
                now = self.clock()
                if (
                    not self.fresh()
                    or not any(m.status == MatchStatus.SCHEDULED and now < m.start_time - timedelta(minutes=5) for m in matches)
                ):
                    return None
            settings = await asyncio.to_thread(get_server_settings, self.guild_id)
            if receipt is None and (not settings.public_reminders_enabled or settings.reminder_channel_id is None):
                return None
            # Create a poll as soon as the source confirms a votable matchup.
            if self.fresh():
                for match in matches:
                    if (
                        match.status == MatchStatus.SCHEDULED and "TBD" not in {match.team1_id, match.team2_id}
                        and self.clock() < match.start_time - timedelta(minutes=5)
                    ):
                        await asyncio.to_thread(polls.create_poll, self.guild_id, match)
            await asyncio.to_thread(polls.reconcile_polls, self.guild_id, self.store.data, self.clock(), fresh=self.fresh())
            poll_by_id = {
                poll.match_id: poll for poll in await asyncio.to_thread(
                    polls.get_polls, self.guild_id, tournament_id,
                )
            }
            mvps = await asyncio.to_thread(get_previous_day_mvp, self.guild_id, self.store.data, day)
            recap = ""
            if mvps:
                names = await asyncio.to_thread(get_voter_display_names, self.guild_id, tournament_id)
                guild = getattr(self.client, "get_guild", lambda _: None)(self.guild_id)
                recap = "\n### __Previous match-day MVP__\n"
                winners = []
                for row in mvps:
                    member = guild.get_member(row.user_id) if guild else None
                    name = member.display_name if member else names.get(row.user_id, f"User {row.user_id}")
                    name = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(name.split())))
                    winners.append(f"**{name}**")
                row = mvps[0]  # Joint winners have the same correct/submitted ratio.
                recap += (f"{', '.join(winners)}: {row.correct}/{row.submitted} correct · {float(row.accuracy):.1%} accuracy\n"
                          f"-# {row.submitted}/{row.eligible} matches predicted · {float(row.participation):.1%} participation\n")
            view = DailyPredictionsView(self, day, matches, poll_by_id, recap)
            signature = hashlib.sha256(json.dumps(view.to_components(), sort_keys=True).encode()).hexdigest()
            channel_id = receipt[0] if receipt else settings.reminder_channel_id
            try:
                if receipt and day not in self._registered:
                    self.client.add_view(view, message_id=receipt[1])
                    self._registered.add(day)
                published = await asyncio.to_thread(polls.get_published_match_ids, self.guild_id, tournament_id)
                open_ids = {m.id for m in matches if m.id in poll_by_id and not poll_by_id[m.id].closed}
                # Verify active boards on restart and periodically, even when
                # their content is unchanged: a moderator may have deleted one.
                check_due = bool(open_ids) and (
                    day not in self._checked_at
                    or self.clock() - self._checked_at[day] >= timedelta(minutes=5)
                )
                if receipt and signature == receipt[2] and open_ids <= published and not check_due:
                    return receipt
                channel = self.client.get_channel(channel_id)
                if channel is None:
                    channel = await self.client.fetch_channel(channel_id)
                validate_reminder_channel(channel, self.guild_id)
                if receipt:
                    message = channel.get_partial_message(receipt[1])
                    try:
                        await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                    except discord.NotFound:
                        if not open_ids or not self.fresh() or not settings.public_reminders_enabled:
                            logger.warning("Daily prediction post %s is missing; no active poll to restore", day)
                            return None
                        # Preserve poll/vote rows and replace only the deleted Discord post.
                        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                else:
                    latest = await asyncio.to_thread(get_server_settings, self.guild_id)
                    if latest != settings or not self.fresh():
                        return None
                    message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                self._posted[day] = (channel_id, message.id, signature)
                self._checked_at[day] = self.clock()
                self._acknowledge_opportunities(matches, poll_by_id)
                await asyncio.to_thread(
                    polls.save_daily_post, self.guild_id, tournament_id, day, channel_id, message.id, signature,
                )
                await self._flush_opportunities()
                return channel_id, message.id, signature
            except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
                logger.exception("Could not update daily prediction post for %s", day)
                self._retry_at[day] = self.clock() + timedelta(seconds=60)
                return None

    async def tick(self):
        await self._flush_opportunities()
        await asyncio.to_thread(polls.reconcile_polls, self.guild_id, self.store.data, self.clock(), fresh=self.fresh())
        days = self.match_days()
        recorded_days = await asyncio.to_thread(polls.get_daily_post_days, self.guild_id, self.store.data.tournament.id)
        for day in sorted(set(days) | recorded_days):
            await self._sync_day(day, days.get(day, []))

    async def prepare_reminder(self, match_id):
        # The database cutoff is exact even if Discord needs time to update the controls.
        await asyncio.to_thread(polls.reconcile_polls, self.guild_id, self.store.data, self.clock(), fresh=self.fresh())
        match = self.store.data.matches_by_id.get(match_id)
        if match is None:
            return ""
        day = match.start_time.astimezone(MATCHDAY_ZONE).date().isoformat()
        try:
            await self._sync_day(day, self.match_days()[day])
        except (discord.DiscordException, OSError, sqlite3.Error, ValueError):
            logger.exception("Could not update daily controls before reminder")
        existing = {p.match_id: p for p in polls.get_polls(self.guild_id, self.store.data.tournament.id)}
        votes = await asyncio.to_thread(polls.get_poll_results, self.guild_id, match_id) if match_id in existing else []
        guild = getattr(self.client, "get_guild", lambda _: None)(self.guild_id)
        if guild:
            votes = [(user_id, chosen, member.display_name if (member := guild.get_member(user_id)) else name)
                     for user_id, chosen, name in votes]
        return format_prediction_results(match, votes)
