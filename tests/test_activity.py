import unittest
from datetime import datetime, timezone

from commands.activity import resolve_activity_period
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


if __name__ == "__main__":
    unittest.main()
