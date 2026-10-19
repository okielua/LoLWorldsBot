"""Restart-safe entry points to personal voting and public participation."""

import asyncio
from datetime import timedelta
import discord

from models import MatchStatus
from services import polls
from services.presentation import message_payload
from services.setup_gate import require_setup
from services.spoilers import matchup
from services.team_history import HistoryPages


def panel_manager(interaction, tournament):
    for manager in (getattr(interaction.client, "prediction_test_manager", None),
                    getattr(interaction.client, "voting_manager", None)):
        if manager and manager.guild_id == interaction.guild_id and manager.store.data.tournament.id == tournament:
            return manager
    return None


def voter_lines(rows):
    names = ["@" + discord.utils.escape_mentions(discord.utils.escape_markdown(
        " ".join((name or f"User {user_id}").split())[:64])) for user_id, name in rows]
    return sorted(names, key=str.casefold)


async def daily_post_reference(guild_id, tournament, day):
    if await asyncio.to_thread(polls.daily_post_suppressed, guild_id, tournament, day):
        return "No daily voting post has been published for today yet."
    receipt = await asyncio.to_thread(polls.get_daily_post, guild_id, tournament, day)
    if receipt is None:
        return "No daily voting post has been published for today yet."
    channel_id, message_id, _ = receipt
    return f"[Open today's daily voting post](https://discord.com/channels/{guild_id}/{channel_id}/{message_id})"


class OpenVotePanel(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"worldsvotepanel:(?P<guild>\d+):(?P<tournament>.+)"):
    def __init__(self, guild_id, tournament):
        self.guild_id, self.tournament = guild_id, tournament
        super().__init__(discord.ui.Button(label="Vote / Change My Vote", style=discord.ButtonStyle.primary,
                                          custom_id=f"worldsvotepanel:{guild_id}:{tournament}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), match["tournament"])

    async def callback(self, interaction):
        if not await require_setup(interaction):
            return
        manager = panel_manager(interaction, self.tournament)
        if interaction.guild_id != self.guild_id or manager is None:
            await interaction.response.send_message(**message_payload("This voting panel has expired. Use `/vote` for current matches."), ephemeral=True)
            return
        interaction.extras["vote_panel_manager"] = manager
        await interaction.client.tree.get_command("vote").callback(interaction)


class PublicVotersButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"worldsvoters:(?P<guild>\d+):(?P<tournament>.+)"):
    def __init__(self, guild_id, tournament):
        self.guild_id, self.tournament = guild_id, tournament
        super().__init__(discord.ui.Button(label="See All Voters", style=discord.ButtonStyle.secondary,
                                          custom_id=f"worldsvoters:{guild_id}:{tournament}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), match["tournament"])

    async def callback(self, interaction):
        if not await require_setup(interaction):
            return
        manager = panel_manager(interaction, self.tournament)
        if interaction.guild_id != self.guild_id or manager is None:
            await interaction.response.send_message(**message_payload("This voting panel has expired. Use `/vote` for current matches."), ephemeral=True)
            return
        await interaction.response.defer()
        pages, current = [], "# Who Has Voted"
        for match in sorted(manager.store.data.matches, key=lambda row: row.start_time):
            if match.status != MatchStatus.SCHEDULED or manager.clock() >= match.start_time - timedelta(minutes=5):
                continue
            rows = await asyncio.to_thread(polls.get_poll_participants, self.guild_id, match.id)
            heading = f"\n\n## {matchup(match)}\nThese users cast a vote:\n"
            if match.round_name and "swiss" in match.stage_id.lower():
                heading = f"\n\n## {matchup(match)}\n{match.round_name}\nThese users cast a vote:\n"
            if len(current) + len(heading) > 3000:
                pages.append(current)
                current = "# Who Has Voted"
            current += heading
            for name in voter_lines(rows) or ["No votes yet."]:
                if len(current) + len(name) > 3300:
                    pages.append(current)
                    current = "# Who Has Voted" + heading
                current += name + "\n"
        pages.append(current)
        controls = HistoryPages(pages)
        await interaction.followup.send(**message_payload(pages[0], controls=controls),
                                        allowed_mentions=discord.AllowedMentions.none())
