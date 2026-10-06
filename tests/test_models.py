import unittest
from datetime import UTC, datetime

from models import (
    Match,
    MatchStatus,
    ReminderMode,
    UserSubscription,
)


class TestModels(unittest.TestCase):

    def test_valid_match(self):
        match = Match(
            id="test-match",
            tournament_id="worlds-2025",
            stage_id="worlds-2025-swiss",
            team1_id="T1",
            team2_id="GEN",
            start_time=datetime(
                2025,
                10,
                20,
                16,
                0,
                tzinfo=UTC,
            ),
            best_of=3,
        )

        self.assertEqual(
            match.status,
            MatchStatus.SCHEDULED,
        )

        self.assertEqual(
            match.best_of,
            3,
        )

    def test_match_requires_timezone(self):
        with self.assertRaises(ValueError):
            Match(
                id="bad-match",
                tournament_id="worlds-2025",
                stage_id="worlds-2025-swiss",
                team1_id="T1",
                team2_id="GEN",
                start_time=datetime(
                    2025,
                    10,
                    20,
                    16,
                    0,
                ),
                best_of=3,
            )

    def test_subscription_defaults(self):
        subscription = UserSubscription(
            guild_id=123,
            user_id=456,
        )

        self.assertFalse(
            subscription.subscribed_to_all_matches
        )

        self.assertEqual(
            subscription.followed_team_ids,
            set(),
        )

        self.assertEqual(
            subscription.reminder_mode,
            ReminderMode.SERVER,
        )

        self.assertEqual(
            subscription.reminder_minutes,
            30,
        )


if __name__ == "__main__":
    unittest.main()