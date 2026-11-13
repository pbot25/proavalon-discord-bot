import unittest

from proavalon.vote_chart import build_vote_history_png
from services.advanced_stats_backfill import hammer_from_vote_chart


class AdvancedStatsBackfillTests(unittest.TestCase):
    def test_recovers_m1_hammer_from_archived_chart(self) -> None:
        players = [f"S5Player{index}" for index in range(10)]
        history = {
            player: [["VHreject"] * 5]
            for player in players
        }
        history[players[6]][0][4] = "VHapprove VHleader"
        chart = build_vote_history_png({
            "playerUsernamesOrdered": players,
            "voteHistory": history,
            "missionHistory": ["succeeded"],
            "numFailsHistory": [0],
            "see": {"roles": ["Resistance"] * 10},
        })
        self.assertIsNotNone(chart)
        self.assertEqual(
            hammer_from_vote_chart(chart, players),
            players[6],
        )

    def test_returns_none_without_fifth_proposal(self) -> None:
        players = [f"S5Player{index}" for index in range(10)]
        history = {
            player: [["VHreject"] * 4]
            for player in players
        }
        chart = build_vote_history_png({
            "playerUsernamesOrdered": players,
            "voteHistory": history,
            "missionHistory": ["succeeded"],
            "numFailsHistory": [0],
            "see": {"roles": ["Resistance"] * 10},
        })
        self.assertIsNotNone(chart)
        self.assertIsNone(hammer_from_vote_chart(chart, players))


if __name__ == "__main__":
    unittest.main()
