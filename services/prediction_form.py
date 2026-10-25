"""Rolling scored-pick history, frozen at each results post's match boundary."""

import asyncio

import discord

from services.prediction_stats import get_competition_snapshot
from services.spoilers import render_text


FORM_HEADING = ('### __Last 5 picks__\n'
                '-# Scored predictions across all rounds · Oldest → newest\n'
                '-# ✅ Correct · ❌ Wrong · 🔥 Current streak of 3+; skipped matches do not break it.')


def form_rows(snapshot, participants):
    stats = {row.user_id: row for row in snapshot.rows}
    lines = []
    for user_id, name in sorted(participants.items(), key=lambda item: (item[1].casefold(), item[0])):
        recent = snapshot.recent.get(user_id, [])[:5]
        row = stats.get(user_id)
        if not recent or row is None:
            continue
        icons = ' '.join('✅' if pick.chosen == pick.winner_id else '❌' for pick in reversed(recent))
        safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(' '.join(name.split())[:64]))
        fire = '🔥 ' if row.current_streak >= 3 else ''
        lines.append(f'**@{safe_name}** · {icons} · {fire}Current streak: **{row.current_streak}**')
    return lines


async def add_prediction_form(pages, manager, matches, votes_by_match):
    participants = {uid: name or f'User {uid}' for match in matches
                    for uid, _, name in votes_by_match.get(match.id, [])}
    if not participants:
        return pages
    # Later matches must never rewrite an old post's history or streak.
    boundary = max((match.start_time, match.id) for match in matches)
    ids = {match.id for match in manager.store.data.matches if (match.start_time, match.id) <= boundary}
    snapshot = await asyncio.to_thread(get_competition_snapshot, manager.guild_id,
        manager.store.data.tournament.id, data=manager.store.data, match_ids=ids)
    lines = form_rows(snapshot, participants)
    if not lines:
        return pages
    section = FORM_HEADING + '\n\n' + '\n'.join(lines)
    def fits(text):
        return max(len(text), len(render_text(text))) <= 3600
    pages = list(pages)
    body, marker, footer = pages[0].partition('\n\n-# • Use `/votehistory`')
    merged = body + '\n\n' + section + marker + footer
    if fits(merged):
        pages[0] = merged
        return pages
    # Stay inside the same Discord post if a large voter list needs more room.
    heading = pages[0].splitlines()[0] + '\n\n' + FORM_HEADING
    current = heading
    for line in lines:
        if not fits(current + '\n\n' + line):
            pages.append(current)
            current = heading
        current += '\n\n' + line
    pages.append(current)
    return pages
