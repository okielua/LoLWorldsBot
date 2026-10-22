"""Shared visual rules for command replies and editable setup panels."""

import discord
import logging
from services.team_emojis import team_display
from services.setup_gate import require_setup

BLUE = 0x5865F2
MATCH_GAP = "\u00a0\n\n"
logger = logging.getLogger(__name__)


class SpacedContainer(discord.ui.Container):
    """Use structural spacing at match and footnote component boundaries."""
    def __init__(self, *args, compact_spacing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.compact_spacing = compact_spacing

    def add_item(self, item):
        if isinstance(item, discord.ui.TextDisplay) and self.children:
            gap = item.content.startswith((MATCH_GAP, "\u200b\n\n", "-# "))
            if gap and not isinstance(self.children[-1], discord.ui.Separator):
                if self.compact_spacing:
                    # Eight/nine-match daily boards cannot fit extra separator
                    # components. Braille blank has visible width and is not
                    # whitespace, so Discord retains this empty text line.
                    item.content = "\u2800\n\n" + item.content.lstrip("\u00a0\u200b\n")
                else:
                    super().add_item(discord.ui.Separator(
                        visible=False, spacing=discord.SeparatorSpacing.large,
                    ))
        return super().add_item(item)


class MessageCard(discord.ui.LayoutView):
    """Use the same text renderer as daily voting; preserve existing controls."""
    def __init__(self, text, controls=None):
        super().__init__(timeout=controls.timeout if controls else 900)
        self.controls = controls
        self.expiry_interaction = None
        self.set_content(text)

    def set_content(self, text):
        """Update a reusable card without replacing its callback owner/timer."""
        controls = self.controls
        self.clear_items()
        card = SpacedContainer(accent_colour=discord.Colour(BLUE))
        positions = getattr(controls, "control_after_paragraphs", None)
        if positions:
            paragraphs = text.split("\n\n")
            items = {item.custom_id: item for item in controls.children}
            cursor = 0
            for row, stop in zip(controls.to_components(), positions, strict=True):
                if cursor:
                    card.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
                card.add_item(team_display("\n\n".join(paragraphs[cursor:stop])))
                card.add_item(discord.ui.ActionRow(*(items[item["custom_id"]] for item in row["components"])))
                cursor = stop
            if cursor < len(paragraphs):
                card.add_item(team_display("\n\n".join(paragraphs[cursor:])))
            for item in getattr(controls, "trailing_items", ()):
                card.add_item(item)
            self.add_item(card)
            return
        after_first_row = None
        split_after = getattr(controls, "first_control_after_paragraph", None)
        if split_after:
            paragraphs = text.split("\n\n", split_after)
            if len(paragraphs) > split_after:
                text = "\n\n".join(paragraphs[:split_after])
                after_first_row = paragraphs[split_after]
        card.add_item(team_display(text))
        if controls:
            items = {item.custom_id: item for item in controls.children}
            for index, row in enumerate(controls.to_components()):
                card.add_item(discord.ui.ActionRow(*(items[item["custom_id"]] for item in row["components"])))
                if index == 0 and after_first_row:
                    card.add_item(team_display(after_first_row))
        self.add_item(card)

    async def interaction_check(self, interaction):
        allowed = await self.controls.interaction_check(interaction) if self.controls else True
        if allowed and not getattr(self.controls, "setup_controls", False) and not await require_setup(interaction):
            return False
        if allowed and self.expiry_interaction is not None:
            self.expiry_interaction = interaction
            interaction.extras["expiring_message_card"] = self
        return allowed

    def expire_with(self, interaction):
        previous = getattr(interaction, "extras", {}).pop("expiring_message_card", None)
        if previous and previous is not self:
            previous.expiry_interaction = None
            previous.stop()
        self.expiry_interaction = interaction

    async def on_timeout(self):
        if self.expiry_interaction is not None:
            try:
                await self.expiry_interaction.delete_original_response()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.warning("Could not remove expired setup message", exc_info=True)

    async def on_error(self, interaction, error, item):
        if self.controls:
            await self.controls.on_error(interaction, error, item)
        else:
            await super().on_error(interaction, error, item)


def message_payload(text, *, controls=None, title_level=2):
    """Use one underlined title, a blue accent, and normal body text."""
    lines = str(text).strip().splitlines()
    title = None
    if lines and lines[0].startswith("#"):
        title = lines.pop(0).lstrip("# ").strip()
        if not (title.startswith("__") and title.endswith("__")):
            title = f"__{title}__"
    description = "\n".join(lines).strip()
    body = f"{'#' * title_level} {title}\n\n{description}" if title else description
    card = getattr(controls, "_message_card", None) if getattr(controls, "reuse_message_card", False) else None
    if card is None:
        card = MessageCard(body, controls)
        if getattr(controls, "reuse_message_card", False):
            controls._message_card = card
    else:
        card.set_content(body)
    return {"content": None, "embeds": [], "view": card}


def embed_card_payload(embed, *, controls=None, title_level=2):
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
    return message_payload("\n\n".join(parts), controls=controls, title_level=title_level)
