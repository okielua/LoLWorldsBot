"""Inspect actual outgoing Discord card text in offline tests."""

from types import SimpleNamespace

import discord


def rendered_payload(payload):
    if "embed" in payload:
        return payload["embed"]
    view = payload["view"]
    assert isinstance(view, discord.ui.LayoutView)
    text = "\n\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
    lines = text.splitlines()
    title = lines.pop(0).lstrip("# ") if lines and lines[0].startswith("#") else None
    return SimpleNamespace(title=title, description="\n".join(lines).strip())


def rendered_message(call):
    return rendered_payload(call.kwargs)
