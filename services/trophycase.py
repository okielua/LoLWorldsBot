"""Aligned trophy collections for every member who has cast a vote."""

import asyncio

import discord

from services.mvp import trophy_cases
from services.prediction_form import aligned_name_fields
from services.prediction_stats import get_voter_display_names
from services.presentation import message_payload
from services.team_history import HistoryPages


TROPHY_HELP = (
    '-# *In total there are 12 trophies to win:*\n'
    '-# *\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}· 8x Swiss matchdays*\n'
    '-# *\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}· 1x Quarterfinal*\n'
    '-# *\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}· 1x Semifinals*\n'
    '-# *\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}· 1x Final*\n'
    '-# *\N{NO-BREAK SPACE}\N{NO-BREAK SPACE}· 1x overall Worlds 2026 voting winner*'
)


def grouped_trophies(case, slots):
    groups, previous = [], None
    for icon, (stage, _) in zip(case, slots):
        group = stage if stage in {'swiss', 'overall'} else 'knockout'
        if group != previous:
            groups.append([])
        groups[-1].append(icon)
        previous = group
    return ('\N{NO-BREAK SPACE}' * 6).join(' '.join(group) for group in groups)


def trophycase_pages(names, cases, slots):
    heading = '# __🏆 Trophy Case__'
    aligned = aligned_name_fields(names)
    lines = [f"{aligned[uid]} · {grouped_trophies(cases.get(uid, ['⚪'] * len(slots)), slots)}"
             for uid in sorted(names, key=lambda uid: (names[uid].casefold(), uid))]
    pages, body = [], heading
    for line in lines or ['No votes have been cast yet.']:
        if len(body) + len(line) + len(TROPHY_HELP) + 4 > 3500:
            pages.append(body + '\n\n' + TROPHY_HELP)
            body = heading
        body += '\n\n' + line
    return pages + [body + '\n\n' + TROPHY_HELP]


async def show_trophycase(interaction, guild_id, tournament_id, *, data=None, ephemeral=False):
    if interaction.guild_id != guild_id or getattr(interaction, 'guild', None) is None:
        await interaction.response.send_message(**message_payload('This trophy case belongs to another server.'), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=ephemeral)
    if data is None:
        manager = getattr(interaction.client, 'voting_manager', None)
        data = manager.store.data if manager and manager.store.data.tournament.id == tournament_id else None
    if data is None:
        await interaction.followup.send(**message_payload('Trophy data is temporarily unavailable.'), ephemeral=True)
        return
    names = await asyncio.to_thread(get_voter_display_names, guild_id, tournament_id)
    for uid in names:
        member = interaction.guild.get_member(uid)
        if member:
            names[uid] = member.display_name
    cases, slots = await asyncio.to_thread(trophy_cases, guild_id, data)
    pages = trophycase_pages(names, cases, slots)
    await interaction.followup.send(**message_payload(pages[0], controls=HistoryPages(pages)),
                                    ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())


class TrophyCaseButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r'worldstrophies:(?P<guild>\d+):(?P<tournament>[^\s]+)'):
    def __init__(self, guild_id, tournament_id):
        self.guild_id, self.tournament_id = guild_id, tournament_id
        super().__init__(discord.ui.Button(label='Trophy Case', emoji='🏆',
                         style=discord.ButtonStyle.secondary,
                         custom_id=f'worldstrophies:{guild_id}:{tournament_id}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['guild']), match['tournament'])

    async def callback(self, interaction):
        await show_trophycase(interaction, self.guild_id, self.tournament_id, ephemeral=False)
