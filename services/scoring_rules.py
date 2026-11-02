"""Shared scoring guide for the slash command and restart-safe shortcuts."""
import discord
from services.presentation import message_payload

RULES = """# __Worlds 2026 Voting Rules__

## Before knockouts
**Correct predictions through Swiss** (including Play-Ins) earn **1 point each**. Wrong or skipped picks earn **0**. These points carry into the overall leaderboard.

## Knockout Bo5 predictions
Choose either team to win **3–0, 3–1 or 3–2**.
• **3 points:** correct winner and exact score.
• **2 points:** correct winner, losing team's score off by one game.
• **1 point:** correct winner, losing team's score off by two games.
• **0 points:** wrong winner or no vote.

**Example:** you pick KC 3–0. KC wins 3–0 → **3 points**; 3–1 → **2**; 3–2 → **1**. If KC loses → **0**.
Older winner-only knockout votes earn **1 point** for the correct winner; no exact score is assumed.

## Voting and standings
You can change your pick until **5 minutes before the scheduled series start**. Only your latest saved pick counts.
**Total points = points through Swiss + knockout prediction points.**
Standings sort by **total points**, then **correct winners**. Remaining ties share a rank.
The fraction and percentage use **submitted, scored predictions**; skipped matches are excluded from that denominator.

## Recent picks and streaks
✅ Correct winner · ❌ Wrong winner · ➖ Skipped.
These track the **winner**, not whether the exact score was right. Skipped matches do not break streaks; 🔥 marks an ongoing streak of **3+**.

## Trophies
**12 trophies:** 8 Swiss matchdays, quarterfinals, semifinals, final and the overall Worlds winner.
Swiss matchday trophies use correct predictions, then percentage, within that matchday.
Knockout stage trophies use that stage's points, then correct winners. The overall trophy uses the overall standings. Tied winners share the trophy; a period with no correct predictions awards none.
"""

async def show_rules(interaction, *, ephemeral=False):
    await interaction.response.send_message(**message_payload(RULES), ephemeral=ephemeral,
                                            allowed_mentions=discord.AllowedMentions.none())

class RulesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"worldsrules:v1"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Rules", emoji="📖", style=discord.ButtonStyle.secondary,
                                          custom_id="worldsrules:v1"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        await show_rules(interaction, ephemeral=True)
