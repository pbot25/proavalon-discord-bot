import unittest
from datetime import datetime, timezone

from commands.activity import (
    _activity_table,
    resolve_activity_period,
    sort_activity_entries,
)
from database.activity import build_activity_stats


class ActivityTests(unittest.TestCase):
    def test_activity_aggregates_games_wins_and_partial_mmr(self):
        rows = [
            {
                "finished_at": "2026-11-10T12:00:00+00:00",
                "winner": "Resistance",
                "players_json": '["Alice", "Bob"]',
                "roles_json": '["Merlin", "Assassin"]',
                "rating_changes_json": '[{"username":"Alice","change":8},{"username":"Bob","change":-12}]',
            },
            {
                "finished_at": "2026-11-11T12:00:00+00:00",
                "winner": "Spy",
                "players_json": '["Alice", "Bob"]',
                "roles_json": '["Resistance", "Morgana"]',
                "rating_changes_json": '[{"username":"Bob","change":7}]',
            },
        ]
        values = build_activity_stats(
            rows,
            start_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
        )
        alice = next(value for value in values if value["username"] == "Alice")
        bob = next(value for value in values if value["username"] == "Bob")
        self.assertEqual((alice["games"], alice["wins"], alice["losses"]), (2, 1, 1))
        self.assertEqual((alice["mmr_change"], alice["mmr_games"]), (8, 1))
        self.assertEqual((bob["games"], bob["wins"], bob["losses"]), (2, 1, 1))
        self.assertEqual((bob["mmr_change"], bob["mmr_games"]), (-5, 2))

    def test_exact_date_range_is_inclusive_of_end_date(self):
        start, end, _, error = resolve_activity_period(
            now=datetime(2026, 11, 15, 17, tzinfo=timezone.utc),
            timezone_name="UTC",
            days=None,
            start_date="2026-11-10",
            end_date="2026-11-12",
        )
        self.assertIsNone(error)
        self.assertEqual(start, datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 13, tzinfo=timezone.utc))

    def test_days_and_start_date_are_mutually_exclusive(self):
        *_, error = resolve_activity_period(
            now=datetime(2026, 11, 15, tzinfo=timezone.utc),
            timezone_name="UTC",
            days=5,
            start_date="2026-11-01",
            end_date=None,
        )
        self.assertIsNotNone(error)

    def test_activity_leaderboard_uses_compact_table(self):
        table = _activity_table(
            [
                {
                    "username": "S5Chipper",
                    "games": 12,
                    "wins": 7,
                    "losses": 5,
                    "win_rate": 58.3,
                    "mmr_change": 24,
                    "mmr_games": 11,
                }
            ],
            start_rank=1,
        )
        self.assertIn("Username", table)
        self.assertIn("GP", table)
        self.assertIn("WR", table)
        self.assertIn("MMR Δ", table)
        self.assertIn("+24", table)
        self.assertNotIn("7W", table)
        self.assertNotIn("captured", table)

    def test_activity_leaderboard_sort_options(self):
        entries = [
            {
                "username": "Games",
                "games": 20,
                "win_rate": 50.0,
                "mmr_change": 5,
                "mmr_games": 20,
            },
            {
                "username": "WinRate",
                "games": 10,
                "win_rate": 70.0,
                "mmr_change": 2,
                "mmr_games": 10,
            },
            {
                "username": "MMR",
                "games": 8,
                "win_rate": 60.0,
                "mmr_change": 30,
                "mmr_games": 8,
            },
        ]
        self.assertEqual(sort_activity_entries(entries, "games")[0]["username"], "Games")
        self.assertEqual(
            sort_activity_entries(entries, "win_rate")[0]["username"],
            "WinRate",
        )
        self.assertEqual(
            sort_activity_entries(entries, "mmr_change")[0]["username"],
            "MMR",
        )


if __name__ == "__main__":
    unittest.main()
