"""Read-only voter history with the shared advanced stats display."""

import asyncio
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from models import MatchStatus
from services.leaderboard_views import show_personal_stats
from services.polls import get_user_votes
from services.prediction_display import my_votes_embeds
from services.prediction_stats import get_voter_display_names
from services.presentation import embed_card_payload, message_payload
from services.user_settings import get_user_timezone, get_user_time_format
from services.setup_gate import SetupRequiredView


async def voter_autocomplete(interaction, current):
    if interaction.guild is None:
        return []
    manager = interaction.client.voting_manager
    names = await asyncio.to_thread(get_voter_display_names, interaction.guild_id, manager.store.data.tournament.id)
    choices = []
    for user_id, saved_name in names.items():
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else saved_name
        if current.casefold() in name.casefold():
            choices.append((name, user_id))
    choices.sort(key=lambda row: (row[0].casefold(), row[1]))
    result = []
    for name, user_id in choices:
        if len(result) == 25:
            break
        try:
            member = interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
        except discord.NotFound:
            continue
        result.append(app_commands.Choice(name=f"{member.display_name} (@{member.name})"[:100], value=str(user_id)))
    return result


class VoteHistoryView(SetupRequiredView):
    def __init__(self, pages, target, guild_id, tournament_id, data, owner_id):
        super().__init__(timeout=900)
        self.pages, self.target, self.page = pages, target, 0
        self.guild_id, self.tournament_id, self.data = guild_id, tournament_id, data
        self.owner_id = owner_id
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page == len(self.pages) - 1

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner_id:
            await interaction.response.send_message(**message_payload("Use `/votehistory` to open your own history view."), ephemeral=True)
            return False
        return True

    async def turn(self, interaction, step):
        self.page = min(max(0, self.page + step), len(self.pages) - 1)
        self.update_buttons()
        await interaction.response.edit_message(**embed_card_payload(self.pages[self.page], controls=self))

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self.turn(interaction, 1)

    @discord.ui.button(label="View Advanced Stats", style=discord.ButtonStyle.primary)
    async def stats(self, interaction, button):
        await show_personal_stats(interaction, self.guild_id, self.tournament_id,
                                  target_user=self.target)


async def show_vote_history(interaction, voter=None):
    await interaction.response.defer(ephemeral=True)
    manager = interaction.client.voting_manager
    data, now = manager.store.data, datetime.now(UTC)
    names = await asyncio.to_thread(get_voter_display_names, interaction.guild_id, data.tournament.id)
    try:
        user_id = int(voter) if voter is not None else interaction.user.id
        if voter is not None and user_id not in names:
            raise ValueError
        target = interaction.user if voter is None else interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
    except (ValueError, discord.NotFound):
        await interaction.followup.send(**message_payload("Choose a server member from the voter suggestions."), ephemeral=True)
        return
    rows = await asyncio.to_thread(get_user_votes, interaction.guild_id, data.tournament.id, user_id)
    # Stored personal picks include open polls. Never reveal another voter's
    # still-private picks, even if their poll has not been reconciled yet.
    rows = [(poll, chosen) for poll, chosen in rows
            if now >= poll.start_time - timedelta(minutes=5)
            or (data.matches_by_id.get(poll.match_id) and
                data.matches_by_id[poll.match_id].status in {MatchStatus.LIVE, MatchStatus.COMPLETED, MatchStatus.CANCELLED})]
    timezone_name = await asyncio.to_thread(get_user_timezone, interaction.user.id)
    time_format = await asyncio.to_thread(get_user_time_format, interaction.user.id)
    pages = my_votes_embeds(rows, data, now=now, display_name=target.display_name,
                           timezone_name=timezone_name, time_format=time_format, guild=interaction.guild, page_size=5)
    safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(target.display_name))
    for index, page in enumerate(pages, 1):
        page.title = f"__Vote History · {safe_name} · {index}/{len(pages)}__"
        page.description = "Picks appear after voting closes."
        if not rows:
            page.description += "\nNo predictions are available to view yet."
        for field_index, field in enumerate(page.fields):
            if field.name != "\u200b":
                page.set_field_at(field_index, name=field.name, value=field.value.replace("Your pick:", "Their pick:"), inline=False)
        page.remove_field(len(page.fields) - 1)
    controls = VoteHistoryView(pages, target, interaction.guild_id, data.tournament.id, data, interaction.user.id)
    if target.id == interaction.user.id:
        controls.stats.label = "My Stats"
    await interaction.followup.send(**embed_card_payload(pages[0], controls=controls), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())
