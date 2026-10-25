"""Rolling scored-pick history, frozen at each results post's match boundary."""

import asyncio
import re
import unicodedata

import discord

from services.prediction_stats import get_competition_snapshot
from services.spoilers import render_text
from services.team_emojis import custom_emoji


FORM_HEADING = ('## __Last 5 picks__\n'
                '-# Oldest → newest\n'
                '-# ✅ Correct · ❌ Wrong · ➖ Skipped\n'
                "-# 🔥 Ongoing streak of 3+ (skipped matches don't break streaks)")


def form_heading():
    return FORM_HEADING.replace('➖', str(custom_emoji('empty') or '➖'))


def aligned_name_fields(names, *, prefix='@', max_length=64):
    def cells(name):
        return sum(0 if unicodedata.category(char) in {'Mn', 'Me', 'Cf'} else
                   2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1 for char in name)
    labels = {uid: prefix + ' '.join(name.split())[:max_length] for uid, name in names.items()}
    width = max(map(cells, labels.values()), default=0)
    result = {}
    for uid, label in labels.items():
        field = discord.utils.escape_mentions(label) + '\N{NO-BREAK SPACE}' * (width - cells(label))
        ticks = '`' * (max((len(run) for run in re.findall(r'`+', field)), default=0) + 1)
        content = f' {field} ' if '`' in field else field
        result[uid] = f'{ticks}{content}{ticks}'
    return result


def form_rows(snapshot, participants):
    stats = {row.user_id: row for row in snapshot.rows}
    participants = {uid: name for uid, name in participants.items() if uid in stats and snapshot.recent.get(uid)}
    names = aligned_name_fields(participants)
    lines = []
    for user_id, name in sorted(participants.items(), key=lambda item: (item[1].casefold(), item[0])):
        recent = snapshot.recent.get(user_id, [])[:5]
        row = stats.get(user_id)
        if not recent or row is None:
            continue
        icons = ' '.join('✅' if pick.chosen == pick.winner_id else '❌' for pick in reversed(recent))
        fire = ' 🔥' if row.current_streak >= 3 else ''
        lines.append(f'{names[user_id]} · {icons} · Current streak: **{row.current_streak}**{fire}')
    return lines


async def add_prediction_form(pages, manager, matches, votes_by_match):
    from services.mvp import add_results_mvp
    participants = {uid: name or f'User {uid}' for match in matches
                    for uid, _, name in votes_by_match.get(match.id, [])}
    if not participants:
        return await add_results_mvp(pages, manager, matches)
    # Later matches must never rewrite an old post's history or streak.
    boundary = max((match.start_time, match.id) for match in matches)
    ids = {match.id for match in manager.store.data.matches if (match.start_time, match.id) <= boundary}
    snapshot = await asyncio.to_thread(get_competition_snapshot, manager.guild_id,
        manager.store.data.tournament.id, data=manager.store.data, match_ids=ids)
    lines = form_rows(snapshot, participants)
    if not lines:
        return await add_results_mvp(pages, manager, matches)
    section = form_heading() + '\n\n' + '\n'.join(lines)
    def fits(text):
        return max(len(text), len(render_text(text))) <= 3600
    pages = list(pages)
    body, marker, footer = pages[0].partition('\n\n-# • Use `/votehistory`')
    merged = body + '\n\n' + section + marker + footer
    if fits(merged):
        pages[0] = merged
        return await add_results_mvp(pages, manager, matches)
    # Stay inside the same Discord post if a large voter list needs more room.
    heading = pages[0].splitlines()[0] + '\n\n' + form_heading()
    current = heading
    for line in lines:
        if not fits(current + '\n\n' + line):
            pages.append(current)
            current = heading
        current += '\n\n' + line
    pages.append(current)
    return await add_results_mvp(pages, manager, matches)
