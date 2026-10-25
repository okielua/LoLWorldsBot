"""Compact leaderboard and personal statistics, sharing ranking and totals."""

import discord

from services.prediction_stats import PredictionStats, AdvancedPredictionStats, competition_ranks
from services.team_emojis import team_text, custom_emoji
from services.footnotes import format_footnotes
from services.spoilers import aggregate_token


ACCENT = 0x5865F2
UNAVAILABLE = "\u2014"  # Approved for unavailable rank and accuracy.
LEADERBOARD_HELP = format_footnotes(
    "-# Standings include play-ins; last 10 picks shown, starting at Swiss.\n"
    "-# ✅ Correct · ❌ Wrong · ➖ Skipped\n"
    "-# 🔥 Ongoing streak of 3+ (skipped matches don't break streaks)\n"
    "-# 🏆 Earned MVP trophies · Full collection: `/trophycase`."
)


def safe_name(name):
    name = " ".join(name.split())
    if len(name) > 80:
        name = name[:79] + "…"
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


def accuracy_text(row):
    if not row.submitted:
        return UNAVAILABLE
    return f"{float(row.accuracy) * 100:.1f}".rstrip("0").rstrip(".") + "%"


def rank_movement(user_id, ranks, previous):
    if previous is None:
        return ""
    if user_id not in previous:
        return "New"
    change = previous[user_id] - ranks[user_id]
    return (f"{custom_emoji('arrow_up_green') or '⬆️'}{change}" if change > 0 else
            f"{custom_emoji('arrow_down_red') or '⬇️'}{-change}" if change < 0 else '')


def leaderboard_embeds(entries, *, previous=None, preview=False, history=None, trophies=None, trophy_slots=None):
    entries = sorted(entries, key=lambda entry: (-entry[0].correct, -entry[0].accuracy, entry[0].user_id))
    ranks = competition_ranks([row for row, _ in entries])
    blocks = []
    history_rows = {row.user_id:row for row in history.rows} if history else {}
    help_text = LEADERBOARD_HELP.replace('➖',str(custom_emoji('dootpinkline') or '➖'))
    slot_count = len(trophy_slots) if trophy_slots is not None else 12
    for row, name in entries:
        movement = rank_movement(row.user_id, ranks, previous)
        streak = getattr(history_rows.get(row.user_id),'current_streak',0) if history is not None else row.current_streak
        streak_icon = ' 🔥' if streak>=3 else ''
        block = (f"# {ranks[row.user_id]}. __{safe_name(name)}__"
                 + (f' {movement}' if movement else '') + streak_icon + '\n'
                 + f'## {row.correct}/{row.submitted} ({accuracy_text(row)})\n')
        if history is not None:
            choices = {pick.match_id:pick.chosen==pick.winner_id for pick in history.recent.get(row.user_id,[])}
            icons = [('✅' if choices[mid] else '❌') if mid in choices else str(custom_emoji('dootpinkline') or '➖')
                     for mid in history.result_match_ids or ()]
        elif preview:
            tail = min(row.current_streak,row.correct)
            icons = (['➖']*max(0,row.eligible-row.submitted) + ['✅']*(row.correct-tail)
                     + ['❌']*(row.submitted-row.correct) + ['✅']*tail)
            icons = [str(custom_emoji('dootpinkline') or '➖') if i=='➖' else i for i in icons]
        else:
            icons = []
        block += ' '.join(icons[-10:])
        case = (trophies or {}).get(row.user_id,['⚪']*slot_count)
        if preview and trophies is None and row.correct:
            case = ['🏆' if i < (2 if row==entries[0][0] else 1) else '⚪' for i in range(slot_count)]
        earned = case.count('🏆')
        if earned:
            block += '\n' + ' '.join(['🏆'] * earned)
        blocks.append(block)
    prefix = "Sample results. The real leaderboard is unchanged.\n\n" if preview else ""
    title = "Communism Worlds Leaderboard"
    if preview:
        title = "TEST: " + title
    pages, body = [], prefix
    # An empty status contains no scored predictions to protect.
    protect = aggregate_token if entries else str
    for block in blocks or ["No results yet. The standings update as matches finish."]:
        if len(body) + len(block) + len(help_text) + 4 > 3700:
            pages.append(discord.Embed(title=title, description=protect(body.rstrip()) + "\n\n" + help_text, colour=ACCENT))
            body = prefix
        body += format_footnotes(block) + "\n\n"
    pages.append(discord.Embed(title=title, description=protect(body.rstrip()) + "\n\n" + help_text, colour=ACCENT))
    return pages


def personal_stats_embed(snapshot, user_id, display_name, *, rows=None, previous=None):
    rows = snapshot.rows if rows is None else rows
    ranks = competition_ranks(rows)
    row = next((row for row in rows if row.user_id == user_id), None)
    title = f"My Worlds Stats · {safe_name(display_name)}"
    if row is None:
        row = PredictionStats(user_id, 0, 0, snapshot.eligible, 0, 0)
    rank = ranks.get(user_id)
    standing = f"{rank} of {len(rows)}" if rank else UNAVAILABLE
    rank_scope = snapshot.result_match_ids
    personal_scope = tuple(prediction.match_id for prediction in snapshot.recent.get(user_id, ()))
    if not personal_scope and row.submitted:
        personal_scope = rank_scope

    def value(text):
        return aggregate_token(str(text), personal_scope)

    body = (
        "-# (All stats cover completed series across Worlds, including Play-Ins.)\n\n"
        f"**My rank: {aggregate_token(standing, rank_scope) if rank else standing}**\n"
        f"**My correct predictions: {value(row.correct)} ({value(accuracy_text(row))})**\n\n"
        f"My votes: **{value(f'{row.submitted} of {row.eligible} past matches')}**\n"
        f"My incorrect predictions: **{value(row.submitted - row.correct)}**\n"
        f"My missed votes: **{value(row.eligible - row.submitted)}**\n\n"
        f"My current streak: **{value(row.current_streak)}**\nMy best streak: **{value(row.longest_streak)}**"
    )
    advanced = snapshot.advanced.get(user_id, AdvancedPredictionStats())

    def team_read(team):
        if team is None:
            return "Not enough data"
        percentage = f"{float(team.accuracy) * 100:.1f}".rstrip("0").rstrip(".")
        return value(team_text(f"**{team.team_id}** · **{team.correct}/{team.scored} correct ({percentage}%)**"))

    backed = advanced.hopium_supplier
    backing_text = value(team_text(f"**{backed.team_id}** · **backed to win in {backed.backed_losses} series they lost**")
                         if backed else "Not enough data")
    minority = (value(f"**{advanced.minority_correct}/{advanced.minority_total} minority predictions correct**")
                if advanced.minority_total else "Not enough data")
    def section(name, title):
        return f"\n\n## {custom_emoji(name) or ':' + name + ':'} __{title}__\n"

    body += (
        section("PeyzPal", "Check Your PayPal")
        + "-# The team whose series I predict most accurately, whether I vote for or against them. Requires at least 3 scored predictions involving that team.\n\n"
        f"{team_read(advanced.best_read)}"
        + section("PepeKMS", "Can’t Read These Frauds")
        + "-# The team whose series I predict least accurately, whether I vote for or against them. Requires at least 3 scored predictions involving that team.\n\n"
        f"{team_read(advanced.hardest)}"
        + section("Copium", "Hopium Supplier")
        + "-# The team I backed to win most often when they actually lost. Only completed series count.\n\n"
        f"{backing_text}"
        + section("Discheesed", "Democracy Was a Mistake")
        + "-# How often I predict correctly when fewer than half the voters on Communism back my chosen team. Evenly split votes don’t count.\n\n"
        f"{minority}"
    )
    return discord.Embed(title=title, description=format_footnotes(body), colour=ACCENT)


def sample_leaderboard_entries(display_name):
    figures = [(18, 24, 4, 7), (18, 26, 0, 6), (18, 26, 3, 5), (14, 20, 2, 5),
               (9, 10, 3, 3), (0, 5, 0, 0)]
    return [(PredictionStats(index, correct, submitted, 30, current, best),
             display_name if index == 1 else f"Sample Player {index}")
            for index, (correct, submitted, current, best) in enumerate(figures, 1)]
