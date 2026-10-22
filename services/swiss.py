"""Swiss record groups from Riot's exact-match bracket descriptions."""

import re


def swiss_round_label(description):
    if not isinstance(description, str):
        return None
    found = re.fullmatch(r"M\d+\s+\(([0-2])-([0-2])\)", description or "")
    if not found:
        return None
    wins, losses = map(int, found.groups())
    label = f"Round {wins + losses + 1} · {wins}–{losses} record"
    if wins == losses == 2:
        label += " · Win to advance, lose to be eliminated"
    elif wins == 2:
        label += " · Advancement match"
    elif losses == 2:
        label += " · Elimination match"
    return label


def round_details(match):
    """Already includes the round and group; does not reveal match outcomes."""
    return f"\n**Swiss Stage · {match.round_name} · Bo{match.best_of}**" if match.round_name and "swiss" in match.stage_id.lower() else ""
