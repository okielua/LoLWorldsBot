"""Shared scoring guide for the slash command and restart-safe shortcuts."""
import discord
from services.presentation import message_payload

RULES = """# 🏆 Worlds 2026 Voting Rules

## Play-Ins → End of Swiss
**1 point** per correct winner

- **Play-Ins: 1 point available** (the bot started late)
- **Swiss: 33 points available**
- **Total: 34 points available**

## Quarterfinals, Semifinals & Final (Bo5)
Voting on the **winner as well as exact Bo5 score**:

- 🎯 Predicting the **exact Bo5 outcome:** **3 points**
- ✅ Predicting the **correct winner, 1 game off:** **2 points**
- ✅ Predicting the **correct winner, 2 games off:** **1 point**
- ❌ Predicting the **wrong winner or skipping:** **0 points**

**Points available:**

- **Quarterfinals: 12 points (4 matches)**
- **Semifinals: 6 points (2 matches)**
- **Final: 3 points (1 match)**
- **Total: 21 points (7 matches)**

### 🥇 Total points & how to win

- **55 points available overall** (34 points from Play-Ins and Swiss + 21 points from knockouts).
- All your points count towards the **overall leaderboard**.
- The player with the **most points wins the overall Worlds 2026 trophy**.
- If points are tied, **the most correct winners predicted** decides. If still tied, players **share the win**.

### 🏆 Trophies

**12 trophies to win:**

- **8 Swiss matchday trophies** (most correct winners predicted that day; ties decided by percentage)
- **1 Quarterfinals trophy** (most points across all 4 matches)
- **1 Semifinals trophy** (most points across both matches)
- **1 Final trophy** (most points in the final)
- **1 Overall Worlds 2026 trophy** (highest total prediction points across Play-Ins, Swiss and knockouts; previous trophy wins don’t count towards it)

For knockout and overall trophies, tied points are decided by **correct winners predicted**. If still tied, players **share the trophy**. Swiss ties remaining after percentage also share the trophy.
**Trophies don’t add points to the leaderboard.** No trophy is awarded for a period with no correct predictions.

### 🔒 Voting closes 5 minutes before the scheduled series start
To change your prediction, **click another prediction button on the voting post** or use **`/vote`**. Your latest saved choice counts.
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
