"""Shared icons and labels for match and prediction states."""

from models import MatchStatus

VOTING_OPEN = "🗳️ Voting open"
VOTING_CLOSED = "🔒 Voting closed"
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
