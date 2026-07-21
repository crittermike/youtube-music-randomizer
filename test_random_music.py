import unittest

from random_music import (
    DiscoveryConfig,
    normalize_text,
    parse_views,
    recording_match_score,
)


class ParsingTests(unittest.TestCase):
    def test_parse_views_supports_abbreviations_and_exact_counts(self):
        self.assertEqual(parse_views("4.1K"), 4_100)
        self.assertEqual(parse_views("2.5M views"), 2_500_000)
        self.assertEqual(parse_views("12,345"), 12_345)
        self.assertIsNone(parse_views(None))

    def test_normalize_text_keeps_unicode_words(self):
        self.assertEqual(normalize_text("  Bjork's SONG!  "), "bjork s song")
        self.assertEqual(normalize_text("東京 事変"), "東京 事変")


class MatchingTests(unittest.TestCase):
    def test_version_suffix_still_matches(self):
        combined, title, artist = recording_match_score(
            "Cleopatra's Needle",
            "Mad Professor",
            "Cleopatra's Needle (Remastered)",
            "Mad Professor",
        )
        self.assertGreater(combined, 0.8)
        self.assertGreater(title, 0.7)
        self.assertEqual(artist, 1.0)

    def test_wrong_artist_is_rejected_by_artist_score(self):
        _, _, artist = recording_match_score(
            "Home",
            "Artist One",
            "Home",
            "Completely Different",
        )
        self.assertLess(artist, 0.45)


class ConfigTests(unittest.TestCase):
    def test_config_parses_and_deduplicates_avoid_terms(self):
        config = DiscoveryConfig.from_payload(
            {
                "count": 12,
                "minViews": 10_000,
                "oldestYear": 1960,
                "newestYear": 2000,
                "avoidTerms": "Live, live\nkaraoke",
            }
        )
        self.assertEqual(config.count, 12)
        self.assertEqual(set(config.avoid_terms), {"live", "karaoke"})

    def test_config_rejects_reversed_years(self):
        with self.assertRaisesRegex(ValueError, "oldest year"):
            DiscoveryConfig.from_payload(
                {"oldestYear": 2000, "newestYear": 1960}
            )


if __name__ == "__main__":
    unittest.main()
