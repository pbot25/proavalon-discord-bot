import unittest

from utils.advanced_stats import m1_hammer_from_vote_history


class AdvancedStatsTests(unittest.TestCase):
    def test_infers_hammer_from_first_proposal_leader(self) -> None:
        players = [f"P{index}" for index in range(10)]
        history = {player: [[""]] for player in players}
        history[players[0]][0][0] = "VHapprove VHleader"
        self.assertEqual(
            m1_hammer_from_vote_history(players, history),
            players[6],
        )

    def test_observed_rotation_controls_hammer_direction(self) -> None:
        players = [f"P{index}" for index in range(10)]
        history = {player: [["", ""]] for player in players}
        history[players[0]][0][0] = "VHreject VHleader"
        history[players[1]][0][1] = "VHapprove VHleader"
        self.assertEqual(
            m1_hammer_from_vote_history(players, history),
            players[4],
        )


if __name__ == "__main__":
    unittest.main()
