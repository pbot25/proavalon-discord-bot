import unittest

from database.seasons import season_uses_linked_accounts


class TournamentSeasonTests(unittest.TestCase):
    def test_private_marker_enables_linked_account_eligibility(self) -> None:
        self.assertTrue(
            season_uses_linked_accounts({"required_prefix": "__linked__5"})
        )

    def test_regular_prefix_keeps_prefix_eligibility(self) -> None:
        self.assertFalse(
            season_uses_linked_accounts({"required_prefix": "S5"})
        )

if __name__ == "__main__":
    unittest.main()
