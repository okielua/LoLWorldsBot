"""Shared display for real and sample Swiss records."""

import discord
from services.spoilers import aggregate_token, current_policy, team_token


def standings_text(data, records, *, preview=False):
    title = f"Worlds {data.tournament.year}: Swiss Standings"
    introduction = ""
    if preview:
        title = "TEST: " + title
        introduction = "Sample teams and records only. The real standings are unchanged.\n\n"
    lines = [f"**{discord.utils.escape_markdown(data.teams_by_id[team_id].short_name)}** · {wins}-{losses}"
             for team_id, wins, losses in records]
    policy = current_policy()
    if policy:
        lines = []
        for team_id, wins, losses in sorted(records, key=lambda row: row[0]):
            fixtures = [match for match in data.matches if team_id in {match.team1_id, match.team2_id}
                        and "swiss" in data.stages_by_id[match.stage_id].name.casefold()]
            first = min(fixtures, key=lambda match: match.start_time, default=None)
            name = team_token(team_id, first.id if first else None, 1 if first and first.team1_id == team_id else 2)
            protected = [match.id for match in fixtures if match.status.value in {"live", "completed"} and policy.protected(match)]
            record = aggregate_token(f"{wins}–{losses}", protected)
            lines.append(f"{name} · {record}")
    body = "\n".join(lines)
    return (f"## {title}\n\n{introduction}" + body
            + "\n\n-# • Series wins-losses; no official tiebreak ranking.")
