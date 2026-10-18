"""Adopt recent messages written before the spoiler-aware renderer existed."""

from copy import deepcopy
import re

from services.spoilers import aggregate_token, current_policy, result_token, score_token, team_token


def matching_fixtures(text):
    policy = current_policy()
    if policy is None:
        return []
    clean = re.sub(r"<a?:\w+:\d+>|[*_`|]", "", text)
    matches = []
    for candidate in policy.data.matches:
        first, second = (policy.data.teams_by_id.get(team_id) for team_id in (candidate.team1_id, candidate.team2_id))
        if first and second and any(f"{left} vs {right}" in clean for left in (first.short_name, first.name)
                                   for right in (second.short_name, second.name)):
            matches.append(candidate)
    return matches


def legacy_text(text, match=None):
    policy = current_policy()
    if policy is None or "⟦" in text:
        return text
    text = re.sub(r"(?im)^.*\bLIVE\b.*\d+[–-]\d+.*$", "🔴 Live · Scores hidden until the series finishes", text)
    matches = matching_fixtures(text)
    if len(matches) == 1:
        match = matches[0]
    elif len(matches) > 1:
        # Ambiguous old rematches must not use an earlier, already released
        # result to make a recent fixture/result visible.
        return aggregate_token(text, [candidate.id for candidate in matches])
    if match:
        lines = []
        for line in text.splitlines():
            if "Result:" in line or "Winner:" in line:
                lines.append("Result: " + score_token(match))
                continue
            if re.search(r"\bLIVE\b", line, re.I) and re.search(r"\d+[–-]\d+", line):
                lines.append("🔴 Live · Scores hidden until the series finishes")
                continue
            if match.status.value == "completed":
                line = re.sub(r"(?:\|\|)?(?:\*\*)?(?<!\d)[0-5][–-][0-5](?!\d)(?:\*\*)?(?:\|\|)?",
                              lambda _: score_token(match), line)
            if re.search(r"\b(?:Correct|Incorrect)\b", line):
                line = re.sub(r"\|\|(\([^\n]*?(?:Correct|Incorrect)[^\n]*?\))\|\|",
                              lambda found: result_token(match, found[1]), line)
            for slot, team_id in enumerate((match.team1_id, match.team2_id), 1):
                team = policy.data.teams_by_id.get(team_id)
                if team is None:
                    continue
                names = "|".join(re.escape(name) for name in sorted({team.name, team.short_name, team.id}, key=len, reverse=True))
                pattern = r"(?:<a?:\w+:\d+>\s*)?(?:\*\*|__)*(?<![\w:/])(?:" + names + r")(?![\w:/])(?:\*\*|__)*(?:\s*<a?:\w+:\d+>)?"
                line = re.sub(pattern, lambda _: team_token(team_id, match.id, slot), line)
            lines.append(line)
        return "\n".join(lines)
    return text


def legacy_payload(draft):
    """Preserve each old card's context, including its native embed fields."""
    draft = deepcopy(draft)

    def visit(value, inherited=None):
        if isinstance(value, list):
            return [visit(item, inherited) for item in value]
        if not isinstance(value, dict):
            return value
        value = dict(value)
        summary = "\n".join(str(value.get(key, "")) for key in ("content", "title", "description", "name"))
        fixtures = matching_fixtures(summary)
        context = fixtures[0] if len(fixtures) == 1 else inherited
        # Full aggregate outputs have no reliably recoverable old input
        # snapshot. Conceal the old table together, including rank order.
        aggregate = bool(re.search(r"(?:Leaderboard|My Worlds Stats|Swiss Standings|Worlds .*Stats)", summary, re.I))
        for key in ("content", "title", "description", "name", "value"):
            text = value.get(key)
            if not isinstance(text, str) or "label" in value:
                continue
            if key == "content":
                # One V2 card may contain several fixture headings.
                blocks = re.split(r"(?m)(?=^#{1,3} [^\n]*\bvs\b)", text)
                value[key] = "".join(legacy_text(block, context) for block in blocks)
            else:
                value[key] = legacy_text(text, context)
            if aggregate and key in {"description", "value", "content"}:
                value[key] = aggregate_token(value[key])
        # Buttons carry an exact series ID even when old human text is unclear.
        custom_id = value.get("custom_id", "")
        found = re.match(r"worldsvote:\d+:(.+):([12])$|worldsday:\d+:(.+):[^:]+:[^:]+:([12])$", custom_id)
        policy = current_policy()
        if found and policy:
            match_id = found[1] or found[3]
            slot = int(found[2] or found[4])
            match = policy.data.matches_by_id.get(match_id)
            if match:
                team_id = match.team1_id if slot == 1 else match.team2_id
                value["label"] = team_token(team_id, match_id, slot)
        for key in ("components", "fields", "embeds"):
            if key in value:
                value[key] = visit(value[key], context)
        return value

    return visit(draft)
