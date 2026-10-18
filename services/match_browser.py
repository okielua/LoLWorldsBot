"""Bounded, three-match pages for upcoming and completed series."""

import discord

from models import MatchStatus
from services.match_times import TOURNAMENT_TIMEZONE
from services.presentation import message_payload
from services.team_history import HistoryPages
from services.user_settings import get_registered_timezones


class MatchPages(HistoryPages):
    def __init__(self, pages, owner_id, command):
        super().__init__(pages)
        self.owner_id, self.command = owner_id, command

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                **message_payload(f"Use `/{self.command}` to browse your own match pages."), ephemeral=True,
            )
            return False
        return True


async def show_match_games(interaction, data, formatter, *, count, upcoming, now):
    if type(count) is not int or not 1 <= count <= 100:
        await interaction.response.send_message(**message_payload("Choose a whole number from 1 to 100."), ephemeral=True)
        return
    matches = sorted(
        (match for match in data.matches
         if match.status == MatchStatus.SCHEDULED and match.start_time > now),
        key=lambda match: (match.start_time, match.id),
    ) if upcoming else sorted(
        (match for match in data.matches if match.status == MatchStatus.COMPLETED and match.start_time <= now),
        key=lambda match: (match.start_time, match.id), reverse=True,
    )
    title = "Next Worlds Matches" if upcoming else "Previous Worlds Matches"
    if not matches:
        empty = "No upcoming matches." if upcoming else "No completed Worlds matches yet."
        await interaction.response.send_message(**message_payload(f"# {title}\n\n{empty}"))
        return
    available = len(matches)
    matches = matches[:count]
    note = (f"Only {available} {'upcoming' if upcoming else 'completed'} matches are available."
            if count > available else "")
    timezones = get_registered_timezones() or [TOURNAMENT_TIMEZONE]
    pages = []
    page_count = (len(matches) + 2) // 3
    for offset in range(0, len(matches), 3):
        parts = [f"# {title}"]
        if note:
            parts.append(note)
        for match in matches[offset:offset + 3]:
            parts.append(formatter(match, TOURNAMENT_TIMEZONE, include_date=True, date_as_heading=True,
                                   now=now, timezones=timezones, hide_result=True, small_details=False))
        if not upcoming:
            parts.append("-# • Recent results use timed spoiler protection. Click a spoiler to reveal it early.")
        if page_count > 1:
            parts.append(f"-# Page {offset // 3 + 1} of {page_count}")
        pages.append("\n\n".join(parts))
    controls = MatchPages(pages, interaction.user.id, "nextgames" if upcoming else "previousgames")
    await interaction.response.send_message(**message_payload(pages[0], controls=controls),
                                            allowed_mentions=discord.AllowedMentions.none())
