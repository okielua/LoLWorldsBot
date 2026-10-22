"""Discord presentation for private picks and revealed prediction results."""

from services.voting_rules import voting_deadline, swiss_batch, batch_help

import discord
from services.team_emojis import team_text, unbold_matchup_title
from services.footnotes import format_footnotes
from datetime import UTC, datetime, timedelta
from math import ceil

from models import MatchStatus
from services.match_times import format_tournament_date, private_match_details
from services.presentation import message_payload
from services.status_labels import (MATCH_STATUS_LABELS, MATCHUP_CHANGED, VOTING_CLOSED, VOTING_OPEN, VOTING_PENDING)
from services.setup_gate import SetupRequiredLayoutView
from services.spoilers import current_policy, matchup, score_token, team_token, result_token
from services.status_labels import upcoming_line


PREDICTION_BLUE = 0x5865F2
VOTING_FOOTER = format_footnotes(
    "-# • Use `/votehistory` for the latest completed series' voting results.\n"
    "-# • Use `/leaderboard` to see Communism's Worlds prediction standings."
)
VOTE_CONFIRMATION_HELP = format_footnotes(
    "-# • Use `/vote` to change your prediction until 5 minutes before a match.\n"
    + VOTING_FOOTER
)


def results_embed(results, *, include_help=True):
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
        embed.add_field(name=team_text(team)[:256], value=value, inline=True)
    # Native footers do not parse inline code. A full-width subtext field keeps
    # the same muted Markdown styling as the other cards, below both columns.
    if include_help:
        embed.add_field(name="\u200b", value=VOTING_FOOTER, inline=False)
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
        label = team_token(team_id, getattr(match, "id", None), index + 1) if current_policy() else team_id
        lines.append(f"**{label}**: {percentage}% ({len(voters)} vote{'s' if len(voters) != 1 else ''})")
        lines.append(", ".join(names) if names else "\u200b")
    return "\n".join(lines)


def votes_match_details(match, stage_name=None):
    stage_name = stage_name or "Stage to be confirmed"
    if stage_name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
        stage_name = "Play-ins"
    details = " · ".join(filter(None, [stage_name, match.round_name if "swiss" in match.stage_id.lower() else None, f"Bo{match.best_of}"]))
    return f"**{discord.utils.escape_markdown(details)}**\n\n{format_tournament_date(match.start_time)}"


def upcoming_votes_embed(match, votes, *, stage_name=None):
    embed = results_embed(format_prediction_results(match, votes), include_help=False)
    details = votes_match_details(match, stage_name)
    embed.description = details + (f"\n\n{embed.description}" if embed.description else "")
    pairing = matchup(match) if current_policy() else discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    embed.title = team_text(f"__Votes: {pairing}__")
    status = MATCH_STATUS_LABELS[match.status]
    if match.status in {MatchStatus.LIVE, MatchStatus.COMPLETED} and match.team1_score is not None and match.team2_score is not None:
        if current_policy():
            if match.status == MatchStatus.COMPLETED:
                status += f" · {score_token(match)}"
        else:
            status += f" · ||{match.team1_score}–{match.team2_score}||"
    embed.add_field(name="\u200b", inline=False,
                    value=status)
    embed.add_field(name="\u200b", value=VOTING_FOOTER, inline=False)
    return unbold_matchup_title(embed)


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
    title = "Your Currently Active Predictions"
    if test_mode:
        title = "TEST: " + title
    pairing = matchup(match) if current_policy() else discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    remaining = voting_time_remaining(voting_deadline(match), now)
    date, time = private_match_details(match.start_time, timezone_name, time_format=time_format)
    return (f"## __{title}__\n\n### {pairing}\n"
            f"{date}\n"
            f"-# {time}\n\n"
            f"{VOTING_OPEN}\n"
            f"-# (Voting closes in {remaining})")


def hidden_votes_payload(match, *, now=None, stage_name=None, no_votes=False, controls=None):
    now = now or datetime.now(UTC)
    deadline = voting_deadline(match)
    pairing = matchup(match) if current_policy() else discord.utils.escape_markdown(f"{match.team1_id} vs {match.team2_id}")
    header = f"## __Votes: {pairing}__\n\n{votes_match_details(match, stage_name)}\n\n"
    if match.status == MatchStatus.SCHEDULED and now < deadline:
        if "TBD" in {match.team1_id, match.team2_id}:
            return message_payload(header + f"**{VOTING_PENDING}**")
        return message_payload(
            header + f"**🗳️ Voting for this match closes in {voting_time_remaining(deadline, now)}**\n\n"
            "-# • Use `/vote` to make or change your prediction for these games", controls=controls)
    return message_payload(header + f"**{VOTING_CLOSED}**" + ("\n\nNo votes were made." if no_votes else ""))


def group_voting_messages(cards):
    """Group match cards, retaining columns and respecting Discord message limits."""
    if not cards:
        return []
    if len(cards) == 1:
        return cards
    # Interactive V2 cards cannot become native embeds without losing buttons.
    # Keep native result columns in separate messages when both formats occur.
    if any("embed" in card for card in cards) and any(
        any(isinstance(item, discord.ui.Button) or isinstance(item, discord.ui.DynamicItem)
            for item in card["view"].walk_children())
        for card in cards if "view" in card
    ):
        messages, batch = [], []
        for card in cards:
            if batch and ("view" in batch[-1]) != ("view" in card):
                messages.extend(group_voting_messages(batch))
                batch = []
            batch.append(card)
        return messages + group_voting_messages(batch)
    messages = []
    if all("view" in card for card in cards):
        view = SetupRequiredLayoutView(timeout=900)
        size = 0
        for payload in cards:
            card, = payload["view"].children
            length = sum(len(item.content) for item in card.walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
            component_count = 1 + sum(1 for _ in card.walk_children())
            if view.children and (size + length > 4000 or view.total_children_count + component_count > 40):
                messages.append({"view": view})
                view, size = SetupRequiredLayoutView(timeout=900), 0
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
        unbold_matchup_title(embed)
        if embeds and (len(embeds) >= 10 or size + len(embed) > 6000):
            messages.append({"embeds": embeds})
            embeds, size = [], 0
        embeds.append(embed)
        size += len(embed)
    messages.append({"embeds": embeds})
    return messages


def prediction_outcome_emojis(guild):
    available = {emoji.name.casefold(): emoji for emoji in getattr(guild, "emojis", ())
                 if getattr(emoji, "available", True)}
    return {key: str(available[name.casefold()]) if name.casefold() in available else f":{name}:"
            for key, name in (("correct", "KingPeepo"), ("incorrect", "peepoSad"))}


def prediction_history_section(poll, data, now):
    match = data.matches_by_id.get(poll.match_id)
    if poll.winner_id or (match and match.status in {MatchStatus.COMPLETED, MatchStatus.CANCELLED}):
        return "🏁 Past Games"
    if (match and match.status == MatchStatus.LIVE) or (poll.start_time <= now and not (match and match.status == MatchStatus.POSTPONED)):
        return "🔴 Ongoing Games"
    return "🕒 Upcoming Games"


def my_votes_embeds(rows, data, *, now=None, display_name=None, timezone_name=None, time_format="24h", guild=None, page_size=10):
    now = now or datetime.now(UTC)
    name = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join((display_name or "You").split())))
    title = f"__@{name}'s Worlds Predictions__"
    emojis = prediction_outcome_emojis(guild)
    sections = {"🏁 Past Games": 0, "🔴 Ongoing Games": 1, "🕒 Upcoming Games": 2}
    rows = sorted(rows, key=lambda row: (sections[prediction_history_section(row[0], data, now)], row[0].start_time, row[0].match_id))
    embeds = []
    for offset in range(0, max(1, len(rows)), page_size):
        embed = discord.Embed(title=title, colour=PREDICTION_BLUE)
        section = None
        for poll, chosen in rows[offset:offset + page_size]:
            group = prediction_history_section(poll, data, now)
            if group != section:
                embed.add_field(name="\u200b", value=f"## __{group}__", inline=False)
                section = group
            match = data.matches_by_id.get(poll.match_id)
            deadline = voting_deadline(match, data) if match else voting_deadline(poll)
            open_for_voting = (not poll.closed and now < deadline
                               and (match is None or match.status == MatchStatus.SCHEDULED))
            state = None
            outcome = ""
            if (chosen is not None and chosen not in {poll.team1_id, poll.team2_id}) or (match and {match.team1_id, match.team2_id} != {poll.team1_id, poll.team2_id}):
                state, result = MATCHUP_CHANGED, ""
            elif poll.winner_id:
                correct = chosen == poll.winner_id
                outcome = f" ||({'Correct' if correct else 'Incorrect'} {emojis['correct' if correct else 'incorrect']})||"
                first = discord.utils.escape_markdown(poll.team1_id)
                second = discord.utils.escape_markdown(poll.team2_id)
                first = f"**__{first}__**" if poll.winner_id == poll.team1_id else f"**{first}**"
                second = f"**__{second}__**" if poll.winner_id == poll.team2_id else f"**{second}**"
                if match and match.team1_score is not None and match.team2_score is not None:
                    score = f"{first} **{match.team1_score}–{match.team2_score}** {second}"
                else:
                    score = f"**__{discord.utils.escape_markdown(poll.winner_id)}__** won"
                result = f"Result: ||{score}||"
            elif match and match.status in {MatchStatus.CANCELLED, MatchStatus.POSTPONED}:
                state, result = MATCH_STATUS_LABELS[match.status], "-# No result"
            else:
                state, result = VOTING_CLOSED, "-# Awaiting result"
                if open_for_voting:
                    state = VOTING_OPEN
                    duration = voting_time_remaining(deadline, now)
                    result = f"-# (Voting closes in {duration})"
            label = discord.utils.escape_markdown(f"{poll.team1_id} vs {poll.team2_id}")
            pick = discord.utils.escape_markdown(chosen) if chosen is not None else None
            if current_policy():
                label = matchup(poll)
                if chosen is not None:
                    slot = 1 if chosen == poll.team1_id else 2
                    pick = team_token(chosen, poll.match_id, slot)
                if poll.winner_id:
                    # Correctness and its emoji must share the result window;
                    # winner emphasis never surrounds either team name.
                    correct = chosen == poll.winner_id
                    outcome = " " + result_token(match, f"({'Correct' if correct else 'Incorrect'} {emojis['correct' if correct else 'incorrect']})") if match else ""
                    result = f"Result: {score_token(match)}" if match else "Result unavailable"
            date, time = private_match_details(poll.start_time, timezone_name, time_format=time_format)
            stage = data.stages_by_id.get(match.stage_id) if match else None
            stage_name = stage.name if stage else "Stage to be confirmed"
            if stage_name.lower().replace("-", "").replace(" ", "") in {"playin", "playins"}:
                stage_name = "Play-ins"
            stage_line = discord.utils.escape_markdown(stage_name + (f" · {match.round_name}" if match and match.round_name and "swiss" in match.stage_id.lower() else "")) + (f" · Bo{match.best_of}" if match else "")
            pick_line = f"Your pick: **{pick}**{outcome}" if pick else ""
            details = "\n".join(line for line in ((result, pick_line) if poll.winner_id else (pick_line, result)) if line)
            embed.add_field(
                name=f"__{label}__",
                value=f"**{stage_line}**\n\n"
                      + (upcoming_line(match, now) + '\n\n' if match and match.status == MatchStatus.SCHEDULED else '')
                      + f"{date}\n-# {time}\n\n"
                      + (f"{state}\n" if state else "")
                      + details,
                inline=False,
            )
        if not rows:
            embed.description = "You haven't made any predictions in this server yet."
        help_text = batch_help(VOTE_CONFIRMATION_HELP) if any(
            (m := data.matches_by_id.get(p.match_id)) and len(swiss_batch(data, m)) > 1 for p, _ in rows) else VOTE_CONFIRMATION_HELP
        embed.add_field(name="\u200b", value=help_text, inline=False)
        embeds.append(embed)
    return embeds
