"""Shared icons and labels for match and prediction states."""

from models import MatchStatus

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
