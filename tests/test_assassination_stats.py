import unittest

from database.assassination_stats import (
    MerlinShotStats,
    assassination_role_bucket,
)


class AssassinationStatsTests(unittest.TestCase):
    def test_role_buckets(self) -> None:
        self.assertEqual(assassination_role_bucket("Resistance"), "VT")
        self.assertEqual(assassination_role_bucket("Tristan"), "Lover")
        self.assertEqual(assassination_role_bucket("Isolde"), "Lover")
        self.assertEqual(assassination_role_bucket("Percival"), "Percival")
        self.assertEqual(assassination_role_bucket("Merlin"), "Merlin")
        self.assertIsNone(assassination_role_bucket("Assassin"))

    def test_false_merlin_shot_totals(self) -> None:
        stats = MerlinShotStats(username="S5Player")
        stats.games_by_role.update({
            "VT": 10,
            "Lover": 4,
            "Percival": 6,
            "Merlin": 5,
        })
        stats.shots_by_role.update({
            "VT": 3,
            "Lover": 1,
            "Percival": 2,
            "Merlin": 4,
        })
        self.assertEqual(stats.non_merlin_games, 20)
        self.assertEqual(stats.false_merlin_shots, 6)
        self.assertEqual(stats.false_shot_rate, 30.0)
        self.assertEqual(stats.total_shots, 10)


if __name__ == "__main__":
    unittest.main()
