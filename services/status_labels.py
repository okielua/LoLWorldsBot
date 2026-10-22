"""Shared icons and labels for match and prediction states."""

from models import MatchStatus
from math import ceil

VOTING_OPEN = "🗳️ Voting open"
VOTING_CLOSED = "🔒 Voting closed"
VOTING_PENDING = "⏳ Voting will open once both teams are confirmed. Subscribe to all-game pings in `/mysetup` to be notified when the day’s voting post goes live."
PAST_PREDICTION = "🏁 Past prediction"
CORRECT = "✅ Correct"
INCORRECT = "❌ Incorrect"
MATCHUP_CHANGED = "⚠️ Matchup changed: prediction no longer valid"

MATCH_STATUS_LABELS = {
    MatchStatus.SCHEDULED: "🕒 UPCOMING",
    MatchStatus.LIVE: "🔴 LIVE",
    MatchStatus.COMPLETED: "🏁 FINISHED",
    MatchStatus.POSTPONED: "⚠️ POSTPONED",
    MatchStatus.CANCELLED: "⛔ CANCELLED",
}


def upcoming_line(match, now):
    label = MATCH_STATUS_LABELS[MatchStatus.SCHEDULED]
    seconds = (match.start_time - now).total_seconds()
    if seconds <= 0:
        return label
    if seconds < 60:
        return label + ' in less than a minute'
    hours, minutes = divmod(ceil(seconds / 60), 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return label + ' in ' + ' '.join(parts)
