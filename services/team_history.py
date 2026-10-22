"""Load a team's full match history only when its button is clicked."""

import discord

from services.presentation import message_payload
from services.setup_gate import SetupRequiredView


class HistoryPages(SetupRequiredView):
    reuse_message_card = True

    def __init__(self, pages):
        super().__init__(timeout=900)
        self.pages, self.page = pages, 0
        if len(pages) == 1:
            self.clear_items()
        else:
            self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page == len(self.pages) - 1

    async def turn(self, interaction, amount):
        # Rendering/transport may wait behind rate limits or spoiler updates.
        # Acknowledge first so Discord's three-second deadline cannot expire.
        await interaction.response.defer()
        self.page = max(0, min(len(self.pages) - 1, self.page + amount))
        self.update_buttons()
        await interaction.edit_original_response(**message_payload(self.pages[self.page], controls=self),
                                                 allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        await self.turn(interaction, 1)


class TeamHistoryView(SetupRequiredView):
    def __init__(self, team_name, load_matches, *, has_matches=True):
        super().__init__(timeout=900)
        self.team_name, self.load_matches = team_name, load_matches
        self.show_all.disabled = not has_matches

    @discord.ui.button(label="Show All Matches", style=discord.ButtonStyle.primary)
    async def show_all(self, interaction, button):
        entries = self.load_matches()
        title = f"## {self.team_name} · All Matches"
        pages, current, stage = [], title, None
        for entry in entries or ["No confirmed matchups are available yet."]:
            if len(current) + len(entry) + 2 > 3400:
                pages.append(current)
                current = title + (f"\n\n{stage}" if stage and not entry.startswith("## __") else "")
            if entry.startswith("## __"):
                stage = entry
            current += "\n\n" + entry
        pages.append(current)
        controls = HistoryPages(pages)
        await interaction.response.send_message(**message_payload(pages[0], controls=controls), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
