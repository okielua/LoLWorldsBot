"""Compact leaderboard and personal statistics, sharing ranking and totals."""

import discord

from services.prediction_stats import PredictionStats, AdvancedPredictionStats, competition_ranks
from services.team_emojis import team_text, custom_emoji
from services.footnotes import format_footnotes


ACCENT = 0x5865F2
UNAVAILABLE = "\u2014"  # Approved for unavailable rank and accuracy.
LEADERBOARD_HELP = format_footnotes(
    "-# • Ranked by correct predictions, then accuracy.\n"
    "-# • ↑↓ Rank movement since the previous completed match.\n"
    "-# • Incorrect predictions reset streaks; missed votes don’t."
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
    return f"↑{change}" if change > 0 else f"↓{-change}" if change < 0 else ""


def leaderboard_embeds(entries, *, previous=None, preview=False):
    entries = sorted(entries, key=lambda entry: (-entry[0].correct, -entry[0].accuracy, entry[0].user_id))
    ranks = competition_ranks([row for row, _ in entries])
    blocks = []
    for row, name in entries:
        movement = rank_movement(row.user_id, ranks, previous)
        streak_icon = "🔥 " if row.current_streak >= 3 else ""
        blocks.append(
            f"### `{ranks[row.user_id]}.` {safe_name(name)}: {row.correct} correct\n"
            + (f"-# Rank movement: {movement}\n" if movement else "")
            + f"-# Voted on {row.submitted} of {row.eligible} past matches\n"
            + f"-# Accuracy: **{accuracy_text(row)}{' correct' if row.submitted else ''}**\n\n"
            + f"-# {streak_icon}Current streak: {row.current_streak}\n-# Best streak: {row.longest_streak}"
        )
    prefix = "Sample results. The real leaderboard is unchanged.\n\n" if preview else ""
    title = "Communism Worlds Leaderboard"
    if preview:
        title = "TEST: " + title
    pages, body = [], prefix
    for block in blocks or ["No results yet. The standings update as matches finish."]:
        if len(body) + len(block) + len(LEADERBOARD_HELP) + 4 > 3700:
            pages.append(discord.Embed(title=title, description=body.rstrip() + "\n\n" + LEADERBOARD_HELP, colour=ACCENT))
            body = prefix
        body += format_footnotes(block) + "\n\n"
    pages.append(discord.Embed(title=title, description=body.rstrip() + "\n\n" + LEADERBOARD_HELP, colour=ACCENT))
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
    body = (
        "-# (All stats cover completed series from Swiss Stage onward.)\n\n"
        f"**My rank: {standing}**\n"
        f"**My correct predictions: {row.correct}**\n\n"
        f"My votes: **{row.submitted} of {row.eligible} past matches**\n"
        f"My accuracy: **{accuracy_text(row)}{' correct' if row.submitted else ''}**\n"
        f"My incorrect predictions: **{row.submitted - row.correct}**\n"
        f"My missed votes: **{row.eligible - row.submitted}**\n\n"
        f"My current streak: **{row.current_streak}**\nMy best streak: **{row.longest_streak}**"
    )
    advanced = snapshot.advanced.get(user_id, AdvancedPredictionStats())

    def team_read(team):
        if team is None:
            return "Not enough data"
        percentage = f"{float(team.accuracy) * 100:.1f}".rstrip("0").rstrip(".")
        return team_text(f"**{team.team_id}** · **{team.correct}/{team.scored} correct ({percentage}%)**")

    backed = advanced.hopium_supplier
    backing_text = (team_text(f"**{backed.team_id}** · **backed to win in {backed.backed_losses} series they lost**")
                    if backed else "Not enough data")
    minority = (f"**{advanced.minority_correct}/{advanced.minority_total} minority predictions correct**"
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
