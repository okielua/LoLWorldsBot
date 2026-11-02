"""Shared scoring guide for the slash command and restart-safe shortcuts."""
import discord
from services.presentation import message_payload

RULES = """# 🏆 Worlds 2026 Voting Rules

## Play-Ins → End of Swiss (34 points to earn)
**1 point** per correct winner

- **Play-Ins: 1 point available** (the bot started late)
- **Swiss: 33 points available**
- **Total: 34 points available**

## Quarterfinals, Semifinals & Final (21 points to earn)
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
