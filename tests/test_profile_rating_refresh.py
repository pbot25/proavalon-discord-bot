import unittest

from proavalon.profile_scraper import current_rating_from_sources


class ProfileRatingRefreshTests(unittest.TestCase):
    def test_current_rating_prefers_fresh_api_value_over_stale_page(self):
        self.assertEqual(
            current_rating_from_sources({"playerRating": 1512}, 1500),
            1512,
        )

    def test_current_rating_uses_page_when_api_omits_rating(self):
        self.assertEqual(current_rating_from_sources({}, 1500), 1500)


if __name__ == "__main__":
    unittest.main()
