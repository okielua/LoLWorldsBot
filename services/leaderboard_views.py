"""Public leaderboard pages and the shared private /votehistory button handler."""

import asyncio
from dataclasses import dataclass

import discord

from services.leaderboard_display import leaderboard_embeds, personal_stats_embed
from services.prediction_stats import competition_ranks, get_competition_snapshot
from services.presentation import message_payload, embed_card_payload
from services.setup_gate import require_setup, SetupRequiredView
from services.trophycase import TrophyCaseButton


@dataclass(frozen=True)
class LeaderboardSnapshot:
    competition: object
    swiss_history: object
    trophy_cases: dict
    trophy_slots: list

    def __getattr__(self, name):
        return getattr(self.competition, name)


async def leaderboard_context(interaction, tournament_id, *, data=None):
    snapshot = await asyncio.to_thread(get_competition_snapshot, interaction.guild_id, tournament_id, data=data)
    if data is not None:
        from services.mvp import trophy_cases
        history = await asyncio.to_thread(get_competition_snapshot, interaction.guild_id,
            tournament_id, data=data, from_swiss=True)
        cases, slots = await asyncio.to_thread(trophy_cases,interaction.guild_id,data)
        snapshot = LeaderboardSnapshot(snapshot,history,cases,slots)
    names = {}
    for row in snapshot.rows + (snapshot.previous_rows or []):
        if row.user_id in names:
            continue
        member = interaction.guild.get_member(row.user_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(row.user_id)
            except discord.NotFound:
                names[row.user_id] = None
                continue
        names[row.user_id] = member.display_name
    entries = [(row, names[row.user_id]) for row in snapshot.rows if names.get(row.user_id) is not None]
    previous = competition_ranks([row for row in snapshot.previous_rows if names.get(row.user_id) is not None]) if snapshot.previous_rows is not None else None
    return snapshot, entries, previous


async def show_personal_stats(interaction, guild_id, tournament_id, *, data=None, target_user=None):
    if interaction.guild_id != guild_id or getattr(interaction, "guild", None) is None:
        await interaction.response.send_message(**message_payload("These stats belong to another server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if data is None:
        manager = getattr(getattr(interaction, "client", None), "voting_manager", None)
        data = manager.store.data if manager and manager.store.data.tournament.id == tournament_id else None
    snapshot, entries, previous = await leaderboard_context(interaction, tournament_id, data=data)
    target = target_user or interaction.user
    payload = embed_card_payload(personal_stats_embed(snapshot, target.id, target.display_name,
                                                     rows=[row for row, _ in entries], previous=previous), title_level=1)
    await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class MyStatsButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"worldsstats:(?P<guild>\d+):(?P<tournament>[^\s]+)"):
    def __init__(self, guild_id, tournament_id):
        self.guild_id, self.tournament_id = guild_id, tournament_id
        super().__init__(discord.ui.Button(label="My Advanced Stats", emoji="🔍", style=discord.ButtonStyle.secondary,
                                          custom_id=f"worldsstats:{guild_id}:{tournament_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), match["tournament"])

    async def callback(self, interaction):
        if not await require_setup(interaction):
            return
        await show_personal_stats(interaction, self.guild_id, self.tournament_id)

    async def interaction_check(self, interaction):
        return await require_setup(interaction)


class LeaderboardView(SetupRequiredView):
    footnote_divider = True
    section_heading_pattern = r'(?m)^# (?:<a?:\w+:\d+>|:(?:challenger|diamond|gold|iron):)$'

    def __init__(self, pages, guild_id, tournament_id):
        super().__init__(timeout=900)
        self.pages, self.page, self.guild_id = pages, 0, guild_id
        self.add_item(MyStatsButton(guild_id, tournament_id))
        self.add_item(TrophyCaseButton(guild_id, tournament_id))
        if len(pages) == 1:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        else:
            self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page == len(self.pages) - 1

    async def turn_page(self, interaction, amount):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(**message_payload("This leaderboard belongs to another server."), ephemeral=True)
            return
        self.page = min(max(0, self.page + amount), len(self.pages) - 1)
        self.update_buttons()
        payload = embed_card_payload(self.pages[self.page], controls=self)
        await interaction.response.edit_message(**payload)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.turn_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self.turn_page(interaction, 1)


def leaderboard_payload(entries, guild_id, tournament_id, *, previous=None, preview=False, context=None):
    pages = leaderboard_embeds(entries, previous=previous, preview=preview,
        history=getattr(context,'swiss_history',None), trophies=getattr(context,'trophy_cases',None),
        trophy_slots=getattr(context,'trophy_slots',None))
    return embed_card_payload(pages[0], controls=LeaderboardView(pages, guild_id, tournament_id))
