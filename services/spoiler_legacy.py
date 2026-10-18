"""Adopt recent messages written before the spoiler-aware renderer existed."""

from copy import deepcopy
import re

from services.spoilers import aggregate_token, current_policy, result_token, score_token, team_token


def navigation_only(text):
    """Recognize result-free bot pages, never a table merely containing a link."""
    clean = re.sub(r"⟦/?aggregate(?::[^⟧]*)?⟧", "", text).replace("||", "").replace("\u200b", "")
    clean = re.sub(r"(?m)^#{1,3}\s*", "", clean).replace("__", "").replace("**", "")
    first = next((line.strip() for line in clean.splitlines() if line.strip()), "")
    if first in {"Worlds Bot: List of all Commands", "Server Setup"}:
        return True
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    status_lines = [line for line in lines if not line.startswith("-#")
                    and line not in {"Communism Worlds Leaderboard", "TEST: Communism Worlds Leaderboard"}]
    if status_lines == ["No results yet. The standings update as matches finish."]:
        return True
    if lines and all(re.match(r"^-#\s*(?:•\s*)?\*?Use `/(?:vote|votehistory|leaderboard|changevote|currentvotes|myvotes|dailyvote)`", line)
                     for line in lines):
        return True
    links = re.findall(r"\[([^\]]+)\]\(<?https?://[^)]+\)", clean)
    remaining = re.sub(r"\[[^\]]+\]\(<?https?://[^)]+\)", "", clean).strip()
    if remaining.casefold() in {"pick'ems leaderboard", "pick’ems leaderboard", "pickems leaderboard"}:
        return True
    return not remaining and any("pick" in label.casefold() and "leaderboard" in label.casefold() for label in links)


def repair_navigation_aggregates(draft):
    """Remove only proven false-positive wrappers from stored navigation cards."""
    def human_text(value):
        if isinstance(value, list):
            return "\n".join(human_text(item) for item in value)
        if not isinstance(value, dict):
            return ""
        return "\n".join(str(item) if isinstance(item, str) else human_text(item)
                         for key, item in value.items()
                         if key in {"content", "title", "description", "name", "value", "text", "fields", "embeds", "components"})

    def visit(value, safe=False):
        if isinstance(value, list):
            return [visit(item, safe) for item in value]
        if not isinstance(value, dict):
            return value
        safe = safe or navigation_only(human_text(value))
        result = {}
        for key, item in value.items():
            if safe and key in {"content", "title", "description", "name", "value", "text"} and isinstance(item, str):
                result[key] = re.sub(r"⟦/?aggregate(?::[^⟧]*)?⟧", "", item).replace("||", "")
            elif key in {"fields", "embeds", "components"}:
                result[key] = visit(item, safe)
            else:
                result[key] = item
        return result
    return visit(draft)


def aggregate_heading(text):
    first = next((line.strip(" #*_`|") for line in text.splitlines() if line.strip()), "")
    if re.search(r"https?://|\]\(|/[a-z]", first, re.I):
        return False
    return bool(re.match(r"^(?:TEST:\s*)?(?:(?:Communism|Worlds|Prediction).*Leaderboard|My Worlds Stats|Worlds .*Stats|(?:Worlds \d+: )?Swiss Standings)(?:\b|$)", first, re.I))


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
        aggregate = aggregate_heading(summary) and not navigation_only(summary)
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
