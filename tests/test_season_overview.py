import unittest

from database.insights import season_overview_summary


class SeasonOverviewTests(unittest.TestCase):
    def test_outcomes_durations_and_coverage(self) -> None:
        rows = [
            {
                "season_match_id": 1,
                "winner": "Resistance",
                "game_duration_seconds": 600,
                "assassination_duration_seconds": 30,
                "mission_history_json": '["succeeded", "failed", "succeeded"]',
                "youtube_url": "https://youtu.be/one",
            },
            {
                "season_match_id": 2,
                "winner": "Spy",
                "game_duration_seconds": 1200,
                "assassination_duration_seconds": None,
                "mission_history_json": '["failed", "failed", "failed"]',
                "youtube_url": None,
            },
            {
                "season_match_id": 3,
                "winner": "Resistance",
                "game_duration_seconds": 900,
                "assassination_duration_seconds": 90,
                "mission_history_json": '["succeeded", "succeeded", "succeeded"]',
                "youtube_url": "https://youtu.be/three",
            },
            {
                "season_match_id": 4,
                "winner": None,
                "game_duration_seconds": None,
                "assassination_duration_seconds": None,
                "mission_history_json": "[]",
                "youtube_url": None,
            },
        ]

        summary = season_overview_summary(rows)
        self.assertEqual(summary["matches"], 4)
        self.assertEqual(summary["resistance_wins"], 2)
        self.assertEqual(summary["spy_wins"], 1)
        self.assertAlmostEqual(summary["resistance_win_rate"], 66.666, places=2)
        self.assertAlmostEqual(summary["spy_win_rate"], 33.333, places=2)
        self.assertEqual(summary["unknown_results"], 1)
        self.assertEqual(summary["average_duration_seconds"], 900)
        self.assertEqual(summary["median_duration_seconds"], 900)
        self.assertEqual(summary["shortest_match_id"], 1)
        self.assertEqual(summary["longest_match_id"], 2)
        self.assertEqual(summary["total_duration_seconds"], 2700)
        self.assertEqual(summary["average_assassination_seconds"], 60)
        self.assertEqual(summary["youtube_matches"], 2)
        self.assertEqual(summary["average_missions"], 2.25)

    def test_empty_season(self) -> None:
        summary = season_overview_summary([])
        self.assertEqual(summary["matches"], 0)
        self.assertIsNone(summary["average_duration_seconds"])
        self.assertEqual(summary["resistance_win_rate"], 0.0)
        self.assertEqual(summary["spy_win_rate"], 0.0)

    def test_m1_hammer_and_card_metrics(self) -> None:
        players = ["ResA", "SpyA"]
        rows = [{
            "season_match_id": 1,
            "winner": "Spy",
            "game_duration_seconds": 600,
            "assassination_duration_seconds": None,
            "mission_history_json": '["failed"]',
            "youtube_url": None,
            "players_json": '["ResA", "SpyA"]',
            "roles_json": '["Resistance", "Assassin"]',
            "vote_history_json": (
                '{"ResA":[["", "", "", "", ""]],'
                '"SpyA":[["", "", "", "", "VHleader"]]}'
            ),
            "card_history_json": '{"Ref of the Rain":["SpyA", "ResA"]}',
        }]
        summary = season_overview_summary(rows)
        self.assertEqual(summary["m1_fails"], 1)
        self.assertEqual(summary["m1_hammer"]["Spy"], 1)
        self.assertEqual(summary["card_starts"]["Spy"], 1)
        self.assertEqual(summary["card_transitions"]["Spy→Resistance"], [1, 1])


if __name__ == "__main__":
    unittest.main()
