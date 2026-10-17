"""Shared display for real and sample Swiss records."""

import discord


def standings_text(data, records, *, preview=False):
    title = f"Worlds {data.tournament.year}: Swiss Standings"
    introduction = ""
    if preview:
        title = "TEST: " + title
        introduction = "Sample teams and records only. The real standings are unchanged.\n\n"
    lines = [f"**{discord.utils.escape_markdown(data.teams_by_id[team_id].short_name)}** · {wins}-{losses}"
             for team_id, wins, losses in records]
    return (f"## {title}\n\n{introduction}" + "\n".join(lines)
            + "\n\n-# • Series wins-losses; no official tiebreak ranking.")
