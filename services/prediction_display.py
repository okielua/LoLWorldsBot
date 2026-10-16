"""Discord presentation for private picks and revealed prediction results."""

import discord
from datetime import UTC, datetime, timedelta
from math import ceil

from models import MatchStatus
from services.match_times import format_tournament_date, private_match_details
from services.presentation import message_payload
from services.status_labels import (CORRECT, INCORRECT, MATCH_STATUS_LABELS,
                                    MATCHUP_CHANGED, PAST_PREDICTION, VOTING_CLOSED, VOTING_OPEN)


PREDICTION_BLUE = 0x5865F2
VOTING_FOOTER = (
    "-# Use `/currentvotes` to see how everyone voted once voting closes.\n"
    "-# Use `/leaderboard` to see Communism's Worlds prediction standings."
)
VOTE_CONFIRMATION_HELP = (
    "-# Use `/changevote` to change your prediction until 5 minutes before a match.\n"
    + VOTING_FOOTER
)


def results_embed(results):
    embed = discord.Embed(title="__Voting Results__", colour=PREDICTION_BLUE)
    lines = results.splitlines()[1:]
    total = 0
    for index in range(0, len(lines), 2):
        heading = lines[index]
        names = lines[index + 1] if index + 1 < len(lines) else "\u200b"
        team, _, statistics = heading.partition(": ")
        percentage, _, count = statistics.partition(" (")
        total += int(count.split(" ", 1)[0])
        value = f"{percentage}\n({count}\n\n{names}"
        if len(value) > 1024:
            names = names[:970].rsplit(", ", 1)[0] + " … (more voters)"
            value = f"{percentage}\n({count}\n\n{names}"
        embed.add_field(name=team[:256], value=value, inline=True)
    # Results alone retain native embeds for columns. Use their smallest native
    # footer rather than a field; Discord controls its exact font weight.
    help_text = "\n".join(line.removeprefix("-# ").replace("`", "") for line in VOTING_FOOTER.splitlines())
    embed.set_footer(text=help_text)
    if total == 0:
        embed.description = "No votes were made."
    return embed


def format_prediction_results(match, votes):
    votes = [vote for vote in votes if vote[1] in {match.team1_id, match.team2_id}]
    total = len(votes)
    lines = ["Prediction results"]
    first_percentage = int(sum(selected == match.team1_id for _, selected, _ in votes) * 100 / total + 0.5) if total else 0
    for index, team_id in enumerate((match.team1_id, match.team2_id)):
        voters = [(uid, name) for uid, selected, name in votes if selected == team_id]
        percentage = first_percentage if index == 0 else (100 - first_percentage if total else 0)
        names = [discord.utils.escape_mentions("@" + discord.utils.escape_markdown(" ".join((name or f"User {uid}").split()))) for uid, name in voters]
        lines.append(f"**{team_id}**: {percentage}% ({len(voters)} vote{'s' if len(voters) != 1 else ''})")
        lines.append(", ".join(names) if names else "\u200b")
    return "\n".join(lines)


def votes_match_details(match, stage_name=None):
    stage_name = stage_name or "Stage to be confirmed"
    if stage_name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
        stage_name = "Play-ins"
    return f"{discord.utils.escape_markdown(stage_name)} · Bo{match.best_of}\n{format_tournament_date(match.start_time)}"


def upcoming_votes_embed(match, votes, *, stage_name=None):
    embed = results_embed(format_prediction_results(match, votes))
    details = votes_match_details(match, stage_name)
    embed.description = details + (f"\n\n{embed.description}" if embed.description else "")
    matchup = discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    embed.title = f"__Votes: {matchup}__"
    status = MATCH_STATUS_LABELS[match.status]
    if match.status in {MatchStatus.LIVE, MatchStatus.COMPLETED} and match.team1_score is not None and match.team2_score is not None:
        status += f" · ||{match.team1_score}–{match.team2_score}||"
    embed.add_field(name="\u200b", inline=False,
                    value=status)
    return embed


def voting_time_remaining(deadline, now):
    seconds = (deadline - now).total_seconds()
    if seconds < 60:
        return "less than a minute"
    hours, minutes = divmod(ceil(seconds / 60), 60)
    return " and ".join(part for part in (
        f"{hours} hour{'s' if hours != 1 else ''}" if hours else "",
        f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else "",
    ) if part)


def change_vote_text(match, *, now, selected_team=None, test_mode=False, timezone_name=None, time_format="24h"):
    title = "Your currently active predictions"
    if test_mode:
        title = "TEST: " + title
    matchup = discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    remaining = voting_time_remaining(match.start_time - timedelta(minutes=5), now)
    date, time = private_match_details(match.start_time, timezone_name, time_format=time_format)
    prompt = "Change your vote here." if selected_team else "Select the team you think will win."
    return (f"## __{title}__\n\n### {matchup}\n"
            f"{date}\n"
            f"-# {time}\n\n"
            f"{VOTING_OPEN}\n"
            f"-# (Voting closes in {remaining})\n\n{prompt}")


def hidden_votes_payload(match, *, now=None, stage_name=None, no_votes=False):
    now = now or datetime.now(UTC)
    deadline = match.start_time - timedelta(minutes=5)
    matchup = discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    header = f"## __Votes: {matchup}__\n\n{votes_match_details(match, stage_name)}\n\n"
    if match.status == MatchStatus.SCHEDULED and now < deadline:
        return message_payload(
            header + f"**🗳️ Voting for this match closes in {voting_time_remaining(deadline, now)}**\n\n"
            "-# Use `/changevote` to make or change your prediction for these games")
    return message_payload(header + f"**{VOTING_CLOSED}**" + ("\n\nNo votes were made." if no_votes else ""))


def group_voting_messages(cards):
    """Group match cards, retaining columns and respecting Discord message limits."""
    if not cards:
        return []
    if len(cards) == 1:
        return cards
    messages = []
    if all("view" in card for card in cards):
        view = discord.ui.LayoutView(timeout=900)
        size = 0
        for payload in cards:
            card, = payload["view"].children
            length = sum(len(item.content) for item in card.walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
            if view.children and (size + length > 4000 or view.total_children_count + 2 > 40):
                messages.append({"view": view})
                view, size = discord.ui.LayoutView(timeout=900), 0
            payload["view"].remove_item(card)
            view.add_item(card)
            size += length
        messages.append({"view": view})
        return messages
    # Native result embeds provide columns. Discord cannot mix them with V2
    # containers, so use matching native cards for placeholders in that response.
    embeds, size = [], 0
    for payload in cards:
        embed = payload.get("embed")
        if embed is None:
            card, = payload["view"].children
            text = "\n\n".join(item.content for item in card.walk_children()
                               if isinstance(item, discord.ui.TextDisplay))
            title, _, body = text.partition("\n")
            embed = discord.Embed(title=title.lstrip("# "), description=body.strip(), colour=PREDICTION_BLUE)
        if embeds and (len(embeds) >= 10 or size + len(embed) > 6000):
            messages.append({"embeds": embeds})
            embeds, size = [], 0
        embeds.append(embed)
        size += len(embed)
    messages.append({"embeds": embeds})
    return messages


def my_votes_embeds(rows, data, *, now=None, display_name=None, timezone_name=None, time_format="24h"):
    now = now or datetime.now(UTC)
    name = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join((display_name or "You").split())))
    title = f"__@{name}'s Worlds Predictions__"
    embeds = []
    for offset in range(0, max(1, len(rows)), 10):
        embed = discord.Embed(title=title, colour=PREDICTION_BLUE)
        for poll, chosen in rows[offset:offset + 10]:
            match = data.matches_by_id.get(poll.match_id)
            deadline = poll.start_time - timedelta(minutes=5)
            open_for_voting = (not poll.closed and now < deadline
                               and (match is None or match.status == MatchStatus.SCHEDULED))
            state = None
            if (chosen is not None and chosen not in {poll.team1_id, poll.team2_id}) or (match and {match.team1_id, match.team2_id} != {poll.team1_id, poll.team2_id}):
                state, result = MATCHUP_CHANGED, ""
            elif poll.winner_id:
                state = PAST_PREDICTION
                outcome = CORRECT if chosen == poll.winner_id else INCORRECT
                if match and match.team1_score is not None and match.team2_score is not None:
                    score = f"{poll.team1_id} {match.team1_score}–{match.team2_score} {poll.team2_id}"
                else:
                    score = f"{poll.winner_id} won"
                result = f"Result: ||{discord.utils.escape_markdown(score)} · {outcome}||"
            elif match and match.status in {MatchStatus.CANCELLED, MatchStatus.POSTPONED}:
                state, result = MATCH_STATUS_LABELS[match.status], "-# No result"
            else:
                state, result = VOTING_CLOSED, "-# Awaiting result"
                if open_for_voting:
                    state = VOTING_OPEN
                    duration = voting_time_remaining(deadline, now)
                    result = f"-# (Voting closes in {duration})"
                    if chosen is not None:
                        result += "\n\n-# Use `/changevote` to change your prediction."
            label = discord.utils.escape_markdown(f"{poll.team1_id} vs {poll.team2_id}")
            pick = discord.utils.escape_markdown(chosen) if chosen is not None else None
            date, time = private_match_details(poll.start_time, timezone_name, time_format=time_format)
            embed.add_field(
                name=label,
                value=f"{date}\n-# {time}\n\n"
                      + (f"{state}\n" if state else "")
                      + (f"Your pick: **{pick}**\n" if pick else "")
                      + result,
                inline=False,
            )
        if not rows:
            embed.description = "You haven't made any predictions in this server yet."
        embeds.append(embed)
    return embeds
