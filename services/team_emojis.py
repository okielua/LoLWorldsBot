"""Resolve server team logos once and use them across Discord presentation."""

import re

import discord
from services.footnotes import format_footnotes


EMOJI_NAMES = {
    "T1": "t1", "TSW": "TSW", "TL": "TLAW", "TLAW": "TLAW",
    "TES": "TES", "MVK": "MVK", "LYON": "LYON", "MKOI": "MKOI",
    "LOS": "LOS", "KC": "KC", "IG": "IG", "HLE": "HLE",
    "GEN": "GENG", "G2": "G2", "FUR": "FURIA", "FURIA": "FURIA",
    "DK": "DKIA", "DKIA": "DKIA", "C9": "CLOUD9", "CLOUD9": "CLOUD9",
    "CFO": "CFO", "BLG": "BLG", "AL": "AL",
}
_emojis = {}
_aliases = dict(EMOJI_NAMES)
_pattern = None
_matchup_pattern = None


def configure_team_emojis(emojis, teams=()):
    """Refresh when Discord connects or the server's emoji collection changes."""
    global _emojis, _aliases, _pattern, _matchup_pattern
    _emojis = {emoji.name.casefold(): emoji for emoji in emojis
               if getattr(emoji, "available", True)}
    _aliases = dict(EMOJI_NAMES)
    for team in teams:
        name = EMOJI_NAMES.get(team.id, EMOJI_NAMES.get(team.short_name))
        if name:
            for alias in (team.id, team.short_name, team.name):
                _aliases[alias] = name
                _aliases[discord.utils.escape_markdown(alias)] = name
    aliases = "|".join(re.escape(alias) for alias in sorted(_aliases, key=len, reverse=True))
    emoji = r"<a?:\w+:\d+>\s*"
    markup = r"(?:\*\*|__)*"
    _matchup_pattern = (
        r"(?<![A-Za-z0-9:])(?:" + emoji + r")?"
        + r"(?P<left>" + markup + r"(?P<left_name>" + aliases + r")" + markup + r")"
        + r"\s+(?P<separator>(?:\*\*|__)*vs(?:\*\*|__)*|(?:\*\*)?\d+[–-]\d+(?:\*\*)?)\s+"
        + r"(?:" + emoji + r")?"
        + r"(?P<right>" + markup + r"(?P<right_name>" + aliases + r")" + markup + r")"
        + r"(?![A-Za-z0-9:])(?:\s*<a?:\w+:\d+>)?"
    )
    # Protect links, mentions, code, and existing custom emoji markup.
    _pattern = re.compile(r"(?P<protected>`+[^`]*`+|https?://\S+|@\w+)"
                          + r"|(?P<matchup>" + _matchup_pattern + r")"
                          + r"|(?P<existing><[^>]+>)"
                          + r"|(?<![A-Za-z0-9:])(?P<team>" + aliases + r")(?![A-Za-z0-9:])")


def team_emoji(team_id):
    name = _aliases.get(team_id)
    return _emojis.get(name.casefold()) if name else None


def custom_emoji(name):
    return _emojis.get(name.casefold())


def team_text(text):
    """Decorate visible team names without changing IDs, URLs, or spoilers."""
    # Matchups are never underlined, including tokenized headings and inline
    # pairings. Run before either rendering branch so shared title formatting
    # cannot reintroduce an underline around padded tags or their emojis.
    def remove_matchup_underline(line):
        return re.sub(r"https?://\S+|<[^>]+>|`[^`]*`|__",
                      lambda part: "" if part[0] == "__" else part[0], line[0])
    text = re.sub(r"(?m)^.*\bvs\b.*$", remove_matchup_underline, text)
    from services.spoilers import current_policy, team_token
    policy = current_policy()
    if policy is not None:
        aliases = {alias: team.id for team in policy.data.teams
                   for alias in (team.id, team.short_name, team.name)}
        names = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        pattern = re.compile(r"(?P<skip>⟦[^⟧]*⟧|https?://\S+|<@[^>]+>|@\w+)|"
                             r"(?P<code>(?:<a?:\w+:\d+>\s*)?`(?P<code_name>[^`\n]+)`)|"
                             r"(?:<a?:\w+:\d+>\s*)?(?:\*\*|__)*(?<![A-Za-z0-9:])"
                             r"(?P<name>" + names + r")(?![A-Za-z0-9:])(?:\*\*|__)*")

        def centered(found):
            if found["skip"]:
                return found[0]
            if found["code"]:
                name = found["code_name"].strip(" \xa0")
                return team_token(aliases[name]) if name in aliases else found[0]
            return team_token(aliases[found["name"]])

        return pattern.sub(centered, text)
    if _pattern is None:
        return text

    # A matchup has its own emphasis; remove the old whole-heading underline.
    def heading(match):
        body = match.group(2)
        return (match.group(1) or "") + body if re.search(_matchup_pattern, body) else match.group()

    text = re.sub(r"^(#{1,3} )?__(.+)__$", heading, text, flags=re.MULTILINE)
    # Keep matchup headings large. Discord also bolds "vs" in headings;
    # inline matchups still emphasize only the teams.

    def replace(match):
        if match.group("protected"):
            return match.group()
        if match.group("existing"):
            following = re.match(r"(?:\*\*|__)*([A-Za-z0-9]+)", text[match.end():])
            return match.group() + (" " if following and team_emoji(following.group(1)) else "")
        if match.group("matchup"):
            left, right = match.group("left_name"), match.group("right_name")
            first, second = team_emoji(left), team_emoji(right)
            first = f"{first} " if first else ""
            second = f" {second}" if second else ""
            left = f"**__{left}__**" if "__" in match.group("left") else f"**{left}**"
            right = f"**__{right}__**" if "__" in match.group("right") else f"**{right}**"
            separator = match.group('separator')
            if separator.strip("*_") == "vs":
                separator = "vs"
            return f"{first}{left} {separator} {right}{second}"
        emoji = team_emoji(match.group("team"))
        if emoji is None:
            return match.group()
        prefix = text[:match.start()].rstrip(" *_~|")
        if prefix.endswith(str(emoji)):
            return match.group()
        return str(emoji) + " " + match.group()

    return _pattern.sub(replace, text)


def team_display(text):
    text = format_footnotes(team_text(text))
    if text.startswith("-# "):
        # Discord trims leading blank lines. Preserve a gap when this text block
        # follows buttons or another component without using extra components.
        text = "\u00a0\n\n" + text
    return discord.ui.TextDisplay(text)


def unbold_matchup_title(embed):
    """Render native matchups as large headings above the match details."""
    if embed.title and " vs " in embed.title:
        title = team_text(embed.title.removeprefix("__").removesuffix("__"))
        embed.description = "## " + title + ("\n\n" + embed.description if embed.description else "")
        embed.title = None
    if embed.description:
        embed.description = format_footnotes(embed.description)
    for index, field in enumerate(embed.fields):
        embed.set_field_at(index, name=field.name, value=format_footnotes(field.value), inline=field.inline)
    return embed


configure_team_emojis(())
