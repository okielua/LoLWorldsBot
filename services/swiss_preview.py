"""Read-only preview of the actual Swiss Round 1 daily voting card."""

from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from services.polls import Poll
from services.setup_gate import SetupRequiredLayoutView
from services.voting import DailyPredictionsView, DailyPageButton, MATCHDAY_ZONE
from services.voting_rules import voting_deadline


class SwissRoundPreview(SetupRequiredLayoutView):
    def __init__(self, data, guild_id, owner_id, *, clock=None):
        super().__init__(timeout=600)
        self.data, self.guild_id, self.owner_id = data, guild_id, owner_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.matches = sorted((m for m in data.matches if 'swiss' in m.stage_id.lower()
                               and (m.round_name or '').startswith('Round 1 ·')),
                              key=lambda m: (m.start_time, m.id))
        if not self.matches:
            raise ValueError('Swiss Round 1 fixtures are not available yet. Refresh the tournament data and try again.')
        self.day = self.matches[0].start_time.astimezone(MATCHDAY_ZONE).date().isoformat()
        self.page = 0
        self.render()

    def render(self):
        self.clear_items()
        manager = SimpleNamespace(guild_id=self.guild_id, test_mode=False, clock=self.clock,
                                  store=SimpleNamespace(data=self.data), _daily_pages={self.day: self.page})
        polls = {m.id: Poll(self.guild_id, m.id, m.tournament_id, m.team1_id, m.team2_id,
                            m.start_time, self.clock() >= voting_deadline(m, self.data), m.winner_id)
                 for m in self.matches}
        original = DailyPredictionsView(manager, self.day, self.matches, polls, participants={})
        self.page_count = original.page_count
        card, = original.children
        header = next(item for item in card.children if isinstance(item, discord.ui.TextDisplay))
        header.content = header.content.replace('# __', '# __PREVIEW: ', 1)
        for row in [child for child in card.children if isinstance(child, discord.ui.ActionRow)]:
            items = row.children
            row.clear_items()
            if any(isinstance(item, DailyPageButton) for item in items):
                previous = discord.ui.Button(label='Previous Matches', style=discord.ButtonStyle.secondary,
                                              disabled=self.page == 0)
                following = discord.ui.Button(label='Next Matches', style=discord.ButtonStyle.primary,
                                               disabled=self.page == self.page_count - 1)
                previous.callback = self.previous_page
                following.callback = self.next_page
                row.add_item(previous)
                row.add_item(following)
            else:
                for item in items:
                    base = item.item if isinstance(item, discord.ui.DynamicItem) else item
                    row.add_item(discord.ui.Button(label=base.label, emoji=base.emoji, style=base.style, disabled=True))
        self.add_item(card)

    async def interaction_check(self, interaction):
        from services.server_setup import can_manage_server
        if interaction.user.id != self.owner_id or not can_manage_server(interaction, self.guild_id):
            from services.presentation import message_payload
            await interaction.response.send_message(**message_payload('Open your own preview through `/serversetup`.'), ephemeral=True)
            return False
        return True

    async def turn(self, interaction, amount):
        await interaction.response.defer()
        self.page = min(max(0, self.page + amount), self.page_count - 1)
        self.render()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def previous_page(self, interaction):
        await self.turn(interaction, -1)

    async def next_page(self, interaction):
        await self.turn(interaction, 1)
