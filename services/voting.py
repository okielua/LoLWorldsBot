"""One private-prediction post per Worlds matchday, with five-minute cutoffs."""

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import sqlite3
from zoneinfo import ZoneInfo

import discord
from services.team_emojis import team_display, team_emoji, team_text
from services.presentation import MATCH_GAP, SpacedContainer, embed_card_payload, message_payload

from models import MatchStatus
from services import polls
from services.match_times import TOURNAMENT_TIMEZONE, private_match_details, registered_match_times
from services.user_settings import get_registered_timezones, get_user_timezone
from services.prediction_display import PREDICTION_BLUE, VOTE_CONFIRMATION_HELP, change_vote_text, format_prediction_results, my_votes_embeds, prediction_history_section
from services.server_reminders import validate_reminder_channel
from services.server_settings import get_server_settings
from services.prediction_stats import get_previous_day_mvp, get_voter_display_names
from services.status_labels import VOTING_CLOSED, VOTING_OPEN, VOTING_PENDING
from services.user_settings import get_all_user_reminder_preferences
from services.reminder_engine import get_server_ping_user_ids
from services.worlds_role import individual_subscribers
from services.setup_gate import require_setup, SetupRequiredView, SetupRequiredLayoutView
from services.spoilers import current_policy, matchup, team_token, aggregate_token

logger = logging.getLogger(__name__)
MATCHDAY_ZONE = ZoneInfo(TOURNAMENT_TIMEZONE)
MAX_DAILY_POST_LEAD = timedelta(hours=24)
VOTING_WINDOW_TARGET = timedelta(hours=8)


class VoteButton(discord.ui.Button):
    def __init__(self, manager, poll, index):
        self.manager, self.poll = manager, poll
        self.team_id = poll.team1_id if index == 1 else poll.team2_id
        super().__init__(
            label=self.team_label(), emoji=team_emoji(self.team_id), style=discord.ButtonStyle.primary, disabled=poll.closed,
            custom_id=f"worldsvote:{poll.guild_id}:{poll.match_id}:{index}",
        )

    def team_label(self):
        # Simulated fixtures are separate from the live spoiler ledger. Their
        # announced participants are visible, so their controls must agree.
        if getattr(self.manager, "test_mode", False):
            return team_token(self.team_id)
        slot = 1 if self.team_id == self.poll.team1_id else 2
        return team_token(self.team_id, self.poll.match_id, slot)

    async def callback(self, interaction):
        if interaction.guild_id != self.poll.guild_id or self.poll.guild_id != self.manager.guild_id:
            await interaction.response.send_message(**message_payload("This poll belongs to another server."), ephemeral=True)
            return
        if not await require_setup(interaction):
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
            stage = self.manager.store.data.stages_by_id.get(getattr(match, "stage_id", None))
            stage_name = stage.name if stage else "Stage to be confirmed"
            if stage_name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
                stage_name = "Play-ins"
            stage_name = discord.utils.escape_markdown(stage_name)
            text = (
                f"Your prediction for **{team1}** vs **{team2}** on {date} ({stage_name}):\n"
                f"## {team}"
            )
            if current_policy():
                # A successful pick is an explicit reveal in this voter's
                # private receipt. Keep public fixtures/buttons protected.
                pairing = f"{team_token(match.team1_id)} vs {team_token(match.team2_id)}"
                text = (f"Your prediction for {pairing} on {date} ({stage_name}):\n"
                        f"## {team_token(self.team_id)}")
            saved = True
        except ValueError as error:
            text = str(error)
        except sqlite3.Error:
            logger.exception("Could not save prediction")
            text = "Couldn't save your prediction. Please try again."
        embed = discord.Embed(description=text, colour=PREDICTION_BLUE)
        if saved:
            embed.add_field(name="\u200b", value=VOTE_CONFIRMATION_HELP, inline=False)
            if isinstance(self.view, PersonalVotesView):
                self.view.select_team(self.poll.match_id, self.team_id)
                if getattr(interaction, "message", None) is not None:
                    try:
                        await interaction.message.edit(view=self.view)
                    except discord.DiscordException:
                        logger.exception("Prediction saved, but private button selection could not be updated")
        await interaction.followup.send(**embed_card_payload(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PersonalVoteView(SetupRequiredView):
    def __init__(self, manager, poll, user_id, selected_team=None):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.add_item(VoteButton(manager, poll, 1))
        self.add_item(VoteButton(manager, poll, 2))
        for button in self.children:
            if button.team_id == selected_team:
                button.label = f"{button.team_label()} ✓"
                button.style = discord.ButtonStyle.success

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/changevote` to make or change your prediction."), ephemeral=True)
            return False
        return True


class ScheduleVoteButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"worldsday:(?P<guild>\d+):(?P<match>.+):(?P<first>[^:]+):(?P<second>[^:]+):(?P<index>[12])"):
    """Restart-safe public buttons retain the exact matchup shown to the voter."""
    def __init__(self, manager, poll, index):
        button = VoteButton(manager, poll, index)
        button.custom_id = f"worldsday:{poll.guild_id}:{poll.match_id}:{poll.team1_id}:{poll.team2_id}:{index}"
        super().__init__(button)

    async def interaction_check(self, interaction):
        return await require_setup(interaction)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        manager = interaction.client.voting_manager
        match_id = match["match"]
        current = manager.store.data.matches_by_id.get(match_id)
        poll = polls.Poll(int(match["guild"]), match_id, manager.store.data.tournament.id,
                          match["first"], match["second"], current.start_time if current else manager.clock(), False, None)
        return cls(manager, poll, int(match["index"]))


class ScheduleVotesView(SetupRequiredLayoutView):
    def __init__(self, manager, title, context, entries, poll_by_id):
        super().__init__(timeout=None)
        self.matches = [match for match, _ in entries]
        card = SpacedContainer(accent_colour=discord.Colour(PREDICTION_BLUE))
        self.add_item(card)
        card.add_item(team_display(f"## {title}\n\n{context.strip()}"))
        for index, (match, text) in enumerate(entries):
            card.add_item(team_display((MATCH_GAP if index else "") + text))
            poll = poll_by_id.get(match.id)
            if poll:
                card.add_item(discord.ui.ActionRow(ScheduleVoteButton(manager, poll, 1), ScheduleVoteButton(manager, poll, 2)))
        card.add_item(team_display(VOTE_CONFIRMATION_HELP))


def current_votes_controls(manager, poll):
    """Public, restart-safe controls; each click saves the clicking user's pick."""
    controls = discord.ui.View(timeout=None)
    controls.first_control_after_paragraph = 3
    controls.add_item(ScheduleVoteButton(manager, poll, 1))
    controls.add_item(ScheduleVoteButton(manager, poll, 2))
    return controls


def schedule_vote_payloads(manager, title, context, entries, poll_by_id):
    pages, page = [], []
    base_size = len(title) + len(context) + len(VOTE_CONFIRMATION_HELP) + 16
    size = base_size
    for match, text in entries:
        entry = (match, team_text(text))
        length = len(entry[1]) + 4
        if page and (len(page) >= 7 or size + length > 3800):
            pages.append({"view": ScheduleVotesView(manager, title, context, page, poll_by_id)})
            page, size = [], base_size
        page.append(entry)
        size += length
    if page:
        pages.append({"view": ScheduleVotesView(manager, title, context, page, poll_by_id)})
    return pages


class PersonalVotesView(SetupRequiredLayoutView):
    """One private card with independently editable controls for every open match."""
    def __init__(self, manager, user_id, rendered):
        super().__init__(timeout=900)
        self.user_id = user_id
        card = SpacedContainer(accent_colour=discord.Colour(PREDICTION_BLUE))
        self.add_item(card)
        title = rendered[0][2].partition("\n\n")[0]
        card.add_item(team_display(title))
        for index, (poll, selected, text) in enumerate(rendered):
            body = text.partition("\n\n")[2]
            card.add_item(team_display((MATCH_GAP if index else "") + body))
            card.add_item(discord.ui.ActionRow(VoteButton(manager, poll, 1), VoteButton(manager, poll, 2)))
            self.select_team(poll.match_id, selected)

    def select_team(self, match_id, team_id):
        for item in self.walk_children():
            if isinstance(item, VoteButton) and item.poll.match_id == match_id:
                selected = item.team_id == team_id
                item.label = item.team_label() + (" ✓" if selected else "")
                item.style = discord.ButtonStyle.success if selected else discord.ButtonStyle.primary

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**message_payload("Use `/changevote` to make or change your prediction."), ephemeral=True)
            return False
        return True


class MyVotesView(PersonalVotesView):
    """Keep prediction history and place the shared controls beneath open picks."""
    def __init__(self, manager, user_id, embed, rows, available):
        discord.ui.LayoutView.__init__(self, timeout=900)
        self.user_id = user_id
        self.pick_texts = {}
        card = SpacedContainer(accent_colour=discord.Colour(PREDICTION_BLUE))
        self.add_item(card)
        card.add_item(team_display(f"## {embed.title}"))
        if embed.description:
            card.add_item(team_display(embed.description))
        matches = iter(rows)
        match_index = 0
        for field in embed.fields:
            if field.name == "\u200b":
                card.add_item(team_display(field.value))
                continue
            poll, selected = next(matches)
            text = team_display((MATCH_GAP if match_index else "") + f"### {field.name}\n{field.value}")
            match_index += 1
            card.add_item(text)
            current = available.get(poll.match_id)
            if current and {poll.team1_id, poll.team2_id} == {current.team1_id, current.team2_id}:
                self.pick_texts[poll.match_id] = text
                card.add_item(discord.ui.ActionRow(VoteButton(manager, current, 1), VoteButton(manager, current, 2)))
                self.select_team(poll.match_id, selected)

    def select_team(self, match_id, team_id):
        super().select_team(match_id, team_id)
        if team_id is not None and match_id in self.pick_texts:
            text = self.pick_texts[match_id]
            lines = [line for line in text.content.splitlines() if not line.startswith("Your pick:")]
            position = next((i for i, line in enumerate(lines) if line.startswith("-#") and "(Voting closes" in line), len(lines))
            policy = current_policy()
            match = policy.data.matches_by_id.get(match_id) if policy else None
            slot = 1 if match and team_id == match.team1_id else 2
            label = team_token(team_id, match_id, slot) if policy else discord.utils.escape_markdown(team_id)
            lines.insert(position, f"Your pick: **{label}**")
            text.content = team_text("\n".join(lines))


def my_vote_payloads(manager, rows, available, user_id, data, *, now, test_mode=False, **preferences):
    sections = {"🏁 Past Games": 0, "🔴 Ongoing Games": 1, "🕒 Upcoming Games": 2}
    rows = sorted(rows, key=lambda row: (sections[prediction_history_section(row[0], data, now)], row[0].start_time, row[0].match_id))
    embeds = my_votes_embeds(rows, data, now=now, page_size=5, **preferences)
    available = {poll.match_id: poll for poll in available}
    for index, embed in enumerate(embeds):
        if test_mode:
            embed.title = "__TEST: " + embed.title.removeprefix("__")
        yield {"view": MyVotesView(manager, user_id, embed, rows[index * 5:(index + 1) * 5], available)}


def personal_vote_payloads(manager, available, user_id, saved, *, timezone_name=None, time_format="24h"):
    selections = {poll.match_id: (poll, chosen) for poll, chosen in saved}
    pages, page, size = [], [], 0
    now = manager.clock()
    for poll in available:
        previous = selections.get(poll.match_id)
        selected = previous[1] if previous and {previous[0].team1_id, previous[0].team2_id} == {poll.team1_id, poll.team2_id} else None
        text = change_vote_text(manager.store.data.matches_by_id[poll.match_id], now=now, selected_team=selected,
                                test_mode=getattr(manager, "test_mode", False), timezone_name=timezone_name, time_format=time_format)
        text = team_text(text)
        if page and (len(page) >= 7 or size + len(text) > 3800):
            pages.append({"view": PersonalVotesView(manager, user_id, page)})
            page, size = [], 0
        page.append((poll, selected, text))
        size += len(text)
    if page:
        pages.append({"view": PersonalVotesView(manager, user_id, page)})
    return pages


class DailyPredictionsView(SetupRequiredLayoutView):
    def __init__(self, manager, day, matches, poll_by_id, recap="", role_id=None, user_ids=()):
        super().__init__(timeout=None)
        # Worlds has at most eight matches per day in the verified schedule.
        # Each match consumes four V2 components, plus the card and two text blocks.
        if len(matches) > 9:
            raise ValueError("This matchday exceeds Discord's single-message component limit.")
        date = datetime.fromisoformat(day)
        timezones = get_registered_timezones()
        card = SpacedContainer(accent_colour=discord.Colour(PREDICTION_BLUE), compact_spacing=len(matches) > 7)
        self.add_item(card)
        data = manager.store.data
        matchdays = sorted({
            match.start_time.astimezone(MATCHDAY_ZONE).date().isoformat()
            for match in data.matches if match.status != MatchStatus.CANCELLED
        } | {day})
        stage_names = []
        for match in matches:
            stage = data.stages_by_id.get(match.stage_id)
            name = stage.name if stage else "Stage to Be Confirmed"
            if name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
                name = "Play-ins"
            if name not in stage_names:
                stage_names.append(name)
        stage_label = discord.utils.escape_markdown(" / ".join(stage_names))
        title = f"Worlds {data.tournament.year} Day {matchdays.index(day) + 1}/{len(matchdays)} · {stage_label}"
        card.add_item(team_display(
            (f"<@&{role_id}>\n" if role_id and not manager.test_mode else "")
            + ((" ".join(f"<@{user_id}>" for user_id in user_ids) + "\n\n") if user_ids and not manager.test_mode else "")
            + f"# __{'TEST: ' if manager.test_mode else ''}{title}__\n\n"
            "Here are today's matchups. Voting for each matchup closes 5 minutes before match start.\n\n"
            "First time here? Run `/mysetup` before voting.\n\n"
            + ("A question-mark button hides a team to avoid spoilers. The left button selects the left team; the right button selects the right team.\n\n" if current_policy() else "")
            + f"## __{date.strftime('%A')}, {date.day} {date.strftime('%B %Y')}__\n"
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
                VOTING_PENDING if "TBD" in {match.team1_id, match.team2_id}
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
            # The container turns this boundary into a structural spacer, or a
            # retained blank glyph line for boards at the component limit.
            gap = MATCH_GAP if match_index else ""
            times = "\n".join(f"-# {line}" for line in registered_match_times(match.start_time, timezones).splitlines())
            card.add_item(team_display(
                f"{gap}## {matchup(match)}\n{stage_label}\n"
                f"{times}\n{status}"
            ))
            card.add_item(discord.ui.ActionRow(VoteButton(manager, poll, 1), VoteButton(manager, poll, 2)))
        card.add_item(team_display(
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
        self._notice_pairs = set()

    async def _followers(self, channel, matches, role_id=None):
        users = await asyncio.to_thread(get_all_user_reminder_preferences)
        recipients = set()
        for match in matches:
            policy = current_policy()
            first = match.team1_id if not policy or not policy.participant_protected(match, 1) else "TBD"
            second = match.team2_id if not policy or not policy.participant_protected(match, 2) else "TBD"
            recipients.update(get_server_ping_user_ids(users, first, second))
        return await individual_subscribers(channel.guild, recipients, role_id)

    async def _notify_new_pairings(self, channel_id, message_id, matches, poll_by_id, enabled, exclude_role_id=None):
        if self.test_mode or not enabled or not self.fresh():
            return
        tournament_id = self.store.data.tournament.id
        if self._notice_pairs:
            await asyncio.to_thread(polls.mark_voting_notices, self.guild_id, tournament_id, self._notice_pairs)
        seen = await asyncio.to_thread(polls.voting_notice_pairs, self.guild_id, tournament_id)
        pending = [match for match in matches
                   if match.id in poll_by_id and not poll_by_id[match.id].closed
                   and self.clock() < match.start_time - timedelta(minutes=5)
                   and (match.id, match.team1_id, match.team2_id) not in seen | self._notice_pairs]
        if not pending:
            return
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        validate_reminder_channel(channel, self.guild_id)
        recipients = await self._followers(channel, pending, exclude_role_id)
        if recipients:
            matchups = "\n".join(f"### {matchup(match)}" for match in pending)
            link = f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"
            for offset in range(0, len(recipients), 90):
                group = recipients[offset:offset + 90]
                text = f"## __Voting Is Open for Your Teams__\n\n{matchups}\n\n[Make your predictions]({link})\n\n" + " ".join(f"<@{user_id}>" for user_id in group)
                await channel.send(**message_payload(text), allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(user_id) for user_id in group], replied_user=False))
        self._notice_pairs.update((match.id, match.team1_id, match.team2_id) for match in pending)
        await asyncio.to_thread(polls.mark_voting_notices, self.guild_id, tournament_id, self._notice_pairs)

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

    async def personal_vote_targets(self, user_id):
        if not self.fresh():
            raise ValueError("Live data is temporarily unavailable; please try again shortly.")
        data = self.store.data
        await asyncio.to_thread(polls.reconcile_polls, self.guild_id, data, self.clock())
        available = await asyncio.to_thread(polls.get_polls, self.guild_id, data.tournament.id)
        published = await asyncio.to_thread(polls.get_published_match_ids, self.guild_id, data.tournament.id)
        candidates = []
        for poll in available:
            match = data.matches_by_id.get(poll.match_id)
            if (match and poll.match_id in published and not poll.closed and match.status == MatchStatus.SCHEDULED
                    and "TBD" not in {match.team1_id, match.team2_id}
                    and self.clock() < match.start_time - timedelta(minutes=5)):
                candidates.append(poll)
        return sorted(candidates, key=lambda poll: (poll.start_time, poll.match_id))

    async def personal_vote_target(self, user_id):
        """Compatibility shortcut for callers that still need a single target."""
        candidates = await self.personal_vote_targets(user_id)
        latest = await asyncio.to_thread(polls.get_latest_vote_match_id, self.guild_id, self.store.data.tournament.id, user_id)
        return min(candidates, key=lambda poll: (poll.match_id != latest, poll.start_time), default=None)

    async def prepare_schedule_polls(self, matches):
        async with self._lock:
            if not self.fresh():
                return {}
            for match in matches:
                if (match.status == MatchStatus.SCHEDULED and "TBD" not in {match.team1_id, match.team2_id}
                        and self.clock() < match.start_time - timedelta(minutes=5)):
                    await asyncio.to_thread(polls.create_poll, self.guild_id, match)
            await asyncio.to_thread(polls.reconcile_polls, self.guild_id, self.store.data, self.clock(), fresh=self.fresh())
            available = await asyncio.to_thread(polls.get_polls, self.guild_id, self.store.data.tournament.id)
            requested = {match.id for match in matches}
            return {poll.match_id: poll for poll in available
                    if poll.match_id in requested and not poll.closed
                    and self.clock() < poll.start_time - timedelta(minutes=5) and self.fresh()}

    async def acknowledge_schedule_opportunities(self, matches, poll_by_id):
        async with self._lock:
            self._acknowledge_opportunities(matches, poll_by_id)
            await self._flush_opportunities()

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

    async def publish_daily_post(self):
        if not self.fresh():
            raise ValueError("Live data is temporarily unavailable. Please try again shortly.")
        days = self.match_days()
        day = next((day for day, matches in days.items() if any(
            match.status not in {MatchStatus.COMPLETED, MatchStatus.CANCELLED} for match in matches)), None)
        if day is None:
            raise ValueError("There is no upcoming matchday to publish.")
        receipt = await self._sync_day(day, days[day], manual=True)
        if receipt is None:
            raise ValueError("The daily post could not be published. Select a reminder channel and check the bot's permissions. "
                             "The matchday must be within 24 hours, earlier matchdays must be finished, and at least one confirmed matchup must still be open for voting.")
        return day, receipt[0], receipt[1]

    async def delete_daily_post(self):
        async with self._lock:
            tournament_id = self.store.data.tournament.id
            latest = await asyncio.to_thread(polls.get_latest_daily_post, self.guild_id, tournament_id, True)
            if latest is None:
                raise ValueError("No daily post has been published in this server.")
            day, channel_id, message_id = latest
            channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
            validate_reminder_channel(channel, self.guild_id)
            was_suppressed = await asyncio.to_thread(polls.daily_post_suppressed, self.guild_id, tournament_id, day)
            # Persist before Discord deletion: a crash cannot cause the automatic
            # recovery path to recreate an intentionally deleted message.
            await asyncio.to_thread(polls.set_daily_post_suppressed, self.guild_id, tournament_id, day, True)
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                await asyncio.to_thread(polls.set_daily_post_suppressed, self.guild_id, tournament_id, day, was_suppressed)
                raise
            await asyncio.to_thread(polls.delete_daily_post_receipt, self.guild_id, tournament_id, day)
            self._posted.pop(day, None)
            self._checked_at.pop(day, None)
            self._retry_at.pop(day, None)
            self._registered.discard(day)
            return day

    async def _sync_day(self, day, matches, *, manual=False):
        async with self._lock:
            if not manual and self.clock() < self._retry_at.get(day, self.clock()):
                return None
            tournament_id = self.store.data.tournament.id
            if not manual and await asyncio.to_thread(polls.daily_post_suppressed, self.guild_id, tournament_id, day):
                return None
            if manual and not self.fresh():
                return None
            receipt = await asyncio.to_thread(polls.get_daily_post, self.guild_id, tournament_id, day)
            if day in self._posted and self._posted[day] != receipt:
                receipt = self._posted[day]
                await asyncio.to_thread(polls.save_daily_post, self.guild_id, tournament_id, day, *receipt)
            if receipt is None and any(
                earlier_day < day and any(
                    match.status not in {MatchStatus.COMPLETED, MatchStatus.CANCELLED}
                    for match in earlier_matches
                )
                for earlier_day, earlier_matches in self.match_days().items()
            ):
                # Unknown opponents and postponed matches still belong to an
                # unfinished matchday. Never skip ahead to a later day's board.
                return None
            if receipt is None:
                starts = [match.start_time for match in matches if match.status != MatchStatus.CANCELLED]
                if not starts or self.clock() < min(starts) - MAX_DAILY_POST_LEAD:
                    return None
            matches = [m for m in matches if "TBD" not in {m.team1_id, m.team2_id}]
            if receipt is None:
                now = self.clock()
                if (
                    not self.fresh()
                    or not any(m.status == MatchStatus.SCHEDULED and now < m.start_time - timedelta(minutes=5) for m in matches)
                ):
                    return None
            settings = await asyncio.to_thread(get_server_settings, self.guild_id)
            if receipt is None and (settings.reminder_channel_id is None or not manual and not settings.public_reminders_enabled):
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
                recap = "\n### __Previous Match-Day MVP__\n"
                winners = []
                for row in mvps:
                    member = guild.get_member(row.user_id) if guild else None
                    name = member.display_name if member else names.get(row.user_id, f"User {row.user_id}")
                    name = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(name.split())))
                    winners.append(f"**{name}**")
                row = mvps[0]  # Joint winners have the same correct/submitted ratio.
                recap += (f"{', '.join(winners)}: {row.correct}/{row.submitted} correct · {float(row.accuracy):.1%} accuracy\n"
                          f"-# {row.submitted}/{row.eligible} matches predicted · {float(row.participation):.1%} participation\n")
                recap = aggregate_token(recap)
            role_id = settings.worlds_role_id if not self.test_mode else None
            view = DailyPredictionsView(self, day, matches, poll_by_id, recap, role_id=role_id)
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
                if not manual and receipt and signature == receipt[2] and open_ids <= published and not check_due:
                    await self._notify_new_pairings(receipt[0], receipt[1], matches, poll_by_id, settings.public_reminders_enabled)
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
                        if not open_ids or not self.fresh() or not manual and not settings.public_reminders_enabled:
                            logger.warning("Daily prediction post %s is missing; no active poll to restore", day)
                            return None
                        # Preserve poll/vote rows and replace only the deleted Discord post.
                        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions(
                            everyone=False, users=False, replied_user=False,
                            roles=[discord.Object(role_id)] if role_id else False,
                        ))
                else:
                    latest = await asyncio.to_thread(get_server_settings, self.guild_id)
                    if latest != settings or not self.fresh():
                        return None
                    open_matches = [match for match in matches if match.id in open_ids]
                    recipients = await self._followers(channel, open_matches, role_id) if not self.test_mode and not manual else []
                    # Large recipient lists use the bounded follow-up path below.
                    inline_followers = len(recipients) <= 90
                    if not inline_followers:
                        recipients = []
                    view = DailyPredictionsView(self, day, matches, poll_by_id, recap, role_id=role_id, user_ids=recipients)
                    if recipients and sum(len(item.content) for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)) > 3900:
                        recipients, inline_followers = [], False
                        view = DailyPredictionsView(self, day, matches, poll_by_id, recap, role_id=role_id)
                    signature = hashlib.sha256(json.dumps(view.to_components(), sort_keys=True).encode()).hexdigest()
                    mentions = discord.AllowedMentions(everyone=False, users=[discord.Object(user_id) for user_id in recipients] if recipients else False, replied_user=False,
                                                       roles=[discord.Object(role_id)] if role_id else False)
                    if not self.fresh() or not any(self.clock() < match.start_time - timedelta(minutes=5) for match in open_matches):
                        return None
                    message = await channel.send(view=view, allowed_mentions=mentions)
                    voting_time = min(match.start_time for match in open_matches) - timedelta(minutes=5) - self.clock()
                    if voting_time < VOTING_WINDOW_TARGET:
                        logger.info("Published matchday %s immediately with %s voting time; eight-hour target could not be met", day, voting_time)
                    if inline_followers and not self.test_mode:
                        self._notice_pairs.update((match.id, match.team1_id, match.team2_id) for match in open_matches)
                self._posted[day] = (channel_id, message.id, signature)
                self._checked_at[day] = self.clock()
                self._acknowledge_opportunities(matches, poll_by_id)
                await asyncio.to_thread(
                    polls.save_daily_post, self.guild_id, tournament_id, day, channel_id, message.id, signature,
                )
                await self._flush_opportunities()
                if manual:
                    self._notice_pairs.update((match.id, match.team1_id, match.team2_id) for match in matches if match.id in open_ids)
                    await asyncio.to_thread(polls.mark_voting_notices, self.guild_id, tournament_id, self._notice_pairs)
                    await asyncio.to_thread(polls.set_daily_post_suppressed, self.guild_id, tournament_id, day, False)
                    self._retry_at.pop(day, None)
                else:
                    await self._notify_new_pairings(channel_id, message.id, matches, poll_by_id, settings.public_reminders_enabled,
                                                   exclude_role_id=role_id if receipt is None else None)
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
