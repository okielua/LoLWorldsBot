import unittest
from pathlib import Path

from services.tournament_data import (
    load_tournament_data,
)


DATA_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "worlds_2025.json"
)


class TestTournamentData(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = load_tournament_data(
            DATA_FILE
        )

    def test_tournament(self):
        self.assertEqual(
            self.data.tournament.id,
            "worlds-2025",
        )

        self.assertEqual(
            self.data.tournament.year,
            2025,
        )

    def test_stage_count(self):
        self.assertEqual(
            len(self.data.stages),
            5,
        )

    def test_team_count(self):
        self.assertEqual(
            len(self.data.teams),
            17,
        )

    def test_match_count(self):
        self.assertEqual(
            len(self.data.matches),
            41,
        )

    def test_t1_exists(self):
        t1 = self.data.teams_by_id["T1"]

        self.assertEqual(
            t1.name,
            "T1",
        )

        self.assertEqual(
            t1.region,
            "LCK",
        )

    def test_final(self):
        final = self.data.matches_by_id[
            "w25-final"
        ]

        self.assertEqual(
            final.team1_id,
            "T1",
        )

        self.assertEqual(
            final.team2_id,
            "KT",
        )

        self.assertEqual(
            final.team1_score,
            3,
        )

        self.assertEqual(
            final.team2_score,
            2,
        )

        self.assertEqual(
            final.winner_id,
            "T1",
        )


if __name__ == "__main__":
    unittest.main()