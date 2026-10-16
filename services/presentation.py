"""Shared visual rules for command replies and editable setup panels."""

import discord

BLUE = 0x5865F2


class MessageCard(discord.ui.LayoutView):
    """Use the same text renderer as daily voting; preserve existing controls."""
    def __init__(self, text, controls=None):
        super().__init__(timeout=controls.timeout if controls else 900)
        self.controls = controls
        card = discord.ui.Container(accent_colour=discord.Colour(BLUE))
        card.add_item(discord.ui.TextDisplay(text))
        if controls:
            items = {item.custom_id: item for item in controls.children}
            for row in controls.to_components():
                card.add_item(discord.ui.ActionRow(*(items[item["custom_id"]] for item in row["components"])))
        self.add_item(card)

    async def interaction_check(self, interaction):
        return await self.controls.interaction_check(interaction) if self.controls else True

    async def on_error(self, interaction, error, item):
        if self.controls:
            await self.controls.on_error(interaction, error, item)
        else:
            await super().on_error(interaction, error, item)


def message_payload(text, *, controls=None):
    """Use one underlined title, a blue accent, and normal body text."""
    lines = str(text).strip().splitlines()
    title = None
    if lines and lines[0].startswith("#"):
        title = lines.pop(0).lstrip("# ").strip()
        if not (title.startswith("__") and title.endswith("__")):
            title = f"__{title}__"
    description = "\n".join(lines).strip()
    body = f"## {title}\n\n{description}" if title else description
    return {"content": None, "embeds": [], "view": MessageCard(body, controls)}


def embed_card_payload(embed, *, controls=None):
    """Convert non-column legacy replies to the normal-weight card renderer."""
    parts = []
    if embed.title:
        parts.append(f"## {embed.title}")
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        if field.name != "\u200b":
            parts.append(f"### {field.name}\n{field.value}")
        else:
            parts.append(field.value)
    return message_payload("\n\n".join(parts), controls=controls)
