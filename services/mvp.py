"""MVP awards for complete Swiss matchdays and knockout stages."""

import asyncio
from dataclasses import dataclass
import re
from zoneinfo import ZoneInfo

import discord

from models import MatchStatus
from services.match_times import TOURNAMENT_TIMEZONE
from services.prediction_stats import get_competition_snapshot, get_voter_display_names
from services.series_results import SWISS_ROUND_MATCH_COUNTS, confirmed_result, swiss_result_groups
from services.presentation import message_payload
from services.team_history import HistoryPages


@dataclass
class MvpAward:
    label: str
    matches: list
    winners: list


def get_mvp_awards(guild_id, data):
    periods = {}
    expected = {}
    labels = {}
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    rounds = swiss_result_groups(data)
    for match in sorted(data.matches, key=lambda m: (m.start_time, m.id)):
        if match.status == MatchStatus.CANCELLED:
            continue
        stage = data.stages_by_id.get(match.stage_id)
        if stage is None:
            continue
        if 'swiss' in match.stage_id.lower():
            number = re.match(r'Round (\d+)\b', match.round_name or '', re.I)
            if number is None or int(number[1]) not in SWISS_ROUND_MATCH_COUNTS:
                continue
            full_round = rounds.get(f'swiss-round:{match.stage_id}:{int(number[1])}', [])
            if len(full_round) != SWISS_ROUND_MATCH_COUNTS[int(number[1])]:
                continue
            day = match.start_time.astimezone(zone).date()
            key = (match.stage_id, int(number[1]), day)
            expected[key] = sum(m.start_time.astimezone(zone).date() == day for m in full_round)
            labels[key] = f'Swiss Round {int(number[1])} · {day:%A, %d %B %Y}'
        else:
            count = next((n for name, n in [('quarterfinal', 4), ('semifinal', 2), ('final', 1)]
                          if name in match.stage_id.lower()), None)
            if count is None:
                continue
            key = (match.stage_id, 0)
            expected[key] = count
            labels[key] = stage.name
        periods.setdefault(key, []).append(match)
    awards = []
    for key, matches in periods.items():
        if len(matches) != expected[key] or not all(confirmed_result(m) for m in matches):
            continue
        rows = get_competition_snapshot(guild_id, data.tournament.id, data=data,
                                        match_ids={m.id for m in matches}).rows
        winners = ([row for row in rows if row.ranking_key == rows[0].ranking_key]
                   if rows and rows[0].correct else [])
        awards.append(MvpAward(labels[key], matches, winners))
    return sorted(awards, key=lambda a: min(m.start_time for m in a.matches))


def previous_mvp_award(guild_id, data, current_day):
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    awards = [award for award in get_mvp_awards(guild_id, data)
              if max(m.start_time.astimezone(zone).date().isoformat() for m in award.matches) < current_day]
    return awards[-1] if awards else None


def trophy_cases(guild_id, data):
    zone = ZoneInfo(TOURNAMENT_TIMEZONE)
    days = sorted({m.start_time.astimezone(zone).date().isoformat() for m in data.matches
                   if 'swiss' in m.stage_id.lower() and m.status != MatchStatus.CANCELLED})
    slots = [('swiss', day) for day in days]
    slots += [(stage.id, '') for stage in sorted(data.stages,key=lambda s:s.order)
              if any(name in stage.id.lower() for name in ('quarterfinal','semifinal','final'))]
    cases = {}
    for award in get_mvp_awards(guild_id,data):
        match = award.matches[0]
        key = (('swiss',match.start_time.astimezone(zone).date().isoformat())
               if 'swiss' in match.stage_id.lower() else (match.stage_id,''))
        if key not in slots:
            continue
        for winner in award.winners:
            case = cases.setdefault(winner.user_id,['⚪']*len(slots))
            case[slots.index(key)] = '🏆'
    return cases, slots


def mvp_pages(awards, names):
    heading = '# __Worlds 2026 MVPs__'
    if not awards:
        return [heading + '\n\nMVPs will appear after each Swiss matchday and knockout stage is complete.']
    pages, body = [], heading
    for award in awards:
        title = f'\n\n## __{award.label} MVP__'
        if not award.winners:
            lines = ['No MVP: no correct predictions were recorded.']
        else:
            row = award.winners[0]
            lines = [f'-# {row.correct}/{row.submitted} correct · {float(row.accuracy):.1%} accuracy']
            for winner in award.winners:
                name = discord.utils.escape_mentions(discord.utils.escape_markdown(
                    ' '.join(names.get(winner.user_id, f'User {winner.user_id}').split())[:80]))
                lines.append(f'## 🏆 {name}')
        if len(body) + len(title) + len(lines[0]) > 3500:
            pages.append(body)
            body = heading
        body += title
        for line in lines:
            if len(body) + len(line) + 2 > 3500:
                pages.append(body)
                body = heading + title + ' (continued)'
            body += '\n\n' + line
    return pages + [body]


async def add_results_mvp(pages, manager, matches):
    """Put the relevant day's award directly below the recent-pick section."""
    from services.spoilers import render_text
    data = manager.store.data
    target_ids = {m.id for m in matches}
    supported = any('swiss' in m.stage_id.lower() or any(name in m.stage_id.lower()
                    for name in ('quarterfinal', 'semifinal', 'final')) for m in matches)
    if not supported:
        return pages
    awards = await asyncio.to_thread(get_mvp_awards, manager.guild_id, data)
    awards = [a for a in awards if target_ids.intersection(m.id for m in a.matches)]
    names = await asyncio.to_thread(get_voter_display_names, manager.guild_id, data.tournament.id)
    blocks = []
    if awards:
        for award in awards:
            swiss = 'swiss' in award.matches[0].stage_id.lower()
            if swiss:
                day = award.matches[0].start_time.astimezone(ZoneInfo(TOURNAMENT_TIMEZONE))
                title = f'## __Matchday MVP · {day:%A, %d %B %Y}__'
            else:
                title = f'## __{award.label} MVP__'
            lines = [title]
            if award.winners:
                for row in award.winners:
                    name = discord.utils.escape_mentions(discord.utils.escape_markdown(
                        ' '.join(names.get(row.user_id, f'User {row.user_id}').split())[:80]))
                    lines.append(f'## 🏆 {name}')
                row = award.winners[0]
                lines.append(f'-# {row.correct}/{row.submitted} correct · {float(row.accuracy):.1%} accuracy')
            else:
                lines.append('No MVP: no correct predictions were recorded.')
            blocks.append(lines)
    else:
        swiss = any('swiss' in m.stage_id.lower() for m in matches)
        blocks = [['## __Matchday MVP__' if swiss else '## __Stage MVP__',
                   'Waiting for all matchday results.' if swiss else 'Waiting for all stage results.']]
    pages = list(pages)
    index = max((i for i, page in enumerate(pages) if '__Last 5 picks__' in page), default=len(pages)-1)
    body, marker, footer = pages[index].partition('\n\n-# • Use `/votehistory`')
    following = []
    for lines in blocks:
        candidate = body + '\n\n' + '\n\n'.join(lines) + marker + footer
        if max(len(candidate), len(render_text(candidate))) > 3600:
            following.append(body + marker + footer)
            body = pages[0].splitlines()[0]
            marker = footer = ''
        for line in lines:
            candidate = body + '\n\n' + line + marker + footer
            if max(len(candidate), len(render_text(candidate))) > 3600:
                following.append(body + marker + footer)
                body = pages[0].splitlines()[0] + '\n\n' + lines[0] + ' (continued)'
                marker = footer = ''
            body += '\n\n' + line
    following.append(body + marker + footer)
    pages[index:index+1] = following
    return pages


async def show_mvps(interaction):
    await interaction.response.defer()
    manager = interaction.client.voting_manager
    if interaction.guild_id != manager.guild_id:
        await interaction.followup.send(**message_payload('These MVPs belong to another server.'),
                                        allowed_mentions=discord.AllowedMentions.none())
        return
    awards = await asyncio.to_thread(get_mvp_awards, interaction.guild_id, manager.store.data)
    names = await asyncio.to_thread(get_voter_display_names, interaction.guild_id, manager.store.data.tournament.id)
    for award in awards:
        for row in award.winners:
            member = interaction.guild.get_member(row.user_id)
            if member:
                names[row.user_id] = member.display_name
    pages = mvp_pages(awards, names)
    await interaction.followup.send(**message_payload(pages[0], controls=HistoryPages(pages), title_level=1),
                                    allowed_mentions=discord.AllowedMentions.none())
