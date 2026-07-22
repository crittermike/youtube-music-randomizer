import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from random_music import (
    CANDIDATE_POOL_MAX_SIZE,
    MUSICBRAINZ_MAX_PAGES,
    MUSICBRAINZ_PAGE_SIZE,
    DiscoveryConfig,
    DiscoveryEngine,
    MusicBrainzSampler,
    RecordingSeed,
    Track,
    YouTubeMusicResolver,
    candidate_pool_size,
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


class CandidatePoolTests(unittest.TestCase):
    def test_pool_scales_with_minimum_views_and_has_a_hard_cap(self):
        self.assertEqual(candidate_pool_size(15, 0), 300)
        self.assertEqual(candidate_pool_size(15, 100_000), 400)
        self.assertEqual(candidate_pool_size(15, 1_000_000), 600)
        self.assertEqual(candidate_pool_size(15, 10_000_000), 800)
        self.assertEqual(
            candidate_pool_size(30, 1_000_000),
            CANDIDATE_POOL_MAX_SIZE,
        )


class MusicBrainzSamplerTests(unittest.TestCase):
    def test_sample_fetches_100_result_pages_and_deduplicates_recordings(self):
        sampler = MusicBrainzSampler()
        calls = []

        def request_page(year, offset, limit):
            page = len(calls)
            calls.append((year, offset, limit))
            recordings = [
                {
                    "id": f"{page}-{index}",
                    "title": f"Song {page}-{index}",
                    "artist-credit": [{"name": f"Artist {page}-{index}"}],
                    "first-release-date": str(year),
                }
                for index in range(limit)
            ]
            if page:
                recordings[0]["id"] = "0-0"
            return {"recordings": recordings}

        sampler._request_page = request_page
        seeds, years = sampler.sample(250, 2000, 2000)

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(years), 3)
        self.assertTrue(
            all(limit == MUSICBRAINZ_PAGE_SIZE for _, _, limit in calls)
        )
        self.assertEqual(len({offset for _, offset, _ in calls}), 3)
        self.assertEqual(len(seeds), 250)
        self.assertEqual(len({seed.mbid for seed in seeds}), 250)

    def test_sample_caps_musicbrainz_page_count(self):
        sampler = MusicBrainzSampler()
        sampler._request_page = Mock(return_value={"recordings": []})

        sampler.sample(10_000, 2000, 2000)

        self.assertEqual(
            sampler._request_page.call_count,
            MUSICBRAINZ_MAX_PAGES,
        )


def make_seed(index):
    return RecordingSeed(
        mbid=f"mbid-{index}",
        title=f"Song {index}",
        artist=f"Source Artist {index}",
        year="2000",
    )


def make_track(index, artist=None, video_id=None):
    return Track(
        video_id=video_id or f"video{index:06d}",
        title=f"Song {index}",
        artist=artist or f"Artist {index}",
        album="Album",
        duration_seconds=180,
        views=1_000_000,
        thumbnail="",
        source_title=f"Song {index}",
        source_artist=f"Source Artist {index}",
        source_year="2000",
        musicbrainz_id=f"mbid-{index}",
        match_score=1.0,
    )


class FakeSampler:
    def __init__(self, seeds):
        self.seeds = seeds
        self.desired = None

    def sample(self, desired, oldest_year, newest_year):
        self.desired = desired
        return self.seeds, [oldest_year, newest_year]


class DiscoveryEngineTests(unittest.TestCase):
    def test_generation_preserves_video_and_artist_uniqueness(self):
        seeds = [make_seed(index) for index in range(5)]
        tracks = {
            "mbid-0": make_track(0, artist="Artist One"),
            "mbid-1": make_track(
                1,
                artist="Artist Two",
                video_id="video000000",
            ),
            "mbid-2": make_track(2, artist="ARTIST ONE"),
            "mbid-3": make_track(3, artist="Artist Three"),
            "mbid-4": make_track(4, artist="Artist Four"),
        }
        resolver = Mock()
        resolver.resolve.side_effect = (
            lambda seed, min_views, avoid_terms: tracks[seed.mbid]
        )
        engine = DiscoveryEngine(
            sampler=FakeSampler(seeds),
            resolver=resolver,
            resolver_workers=3,
            resolution_wave_size=5,
        )

        result = engine.generate(
            DiscoveryConfig(3, 1_000_000, 2000, 2000, ())
        )

        self.assertEqual(result["found"], 3)
        self.assertEqual(
            len({track["videoId"] for track in result["tracks"]}),
            3,
        )
        self.assertEqual(
            len(
                {
                    normalize_text(track["artist"])
                    for track in result["tracks"]
                }
            ),
            3,
        )

    def test_generation_resolves_one_bounded_wave_in_parallel_then_stops(self):
        seeds = [make_seed(index) for index in range(12)]

        class BlockingResolver:
            def __init__(self):
                self.lock = threading.Lock()
                self.release = threading.Event()
                self.calls = 0
                self.active = 0
                self.max_active = 0

            def resolve(self, seed, min_views, avoid_terms):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active == 4:
                        self.release.set()
                if not self.release.wait(1):
                    raise AssertionError("Resolution workers did not run in parallel.")
                with self.lock:
                    self.active -= 1
                return make_track(int(seed.mbid.removeprefix("mbid-")))

        resolver = BlockingResolver()
        sampler = FakeSampler(seeds)
        engine = DiscoveryEngine(
            sampler=sampler,
            resolver=resolver,
            resolver_workers=4,
            resolution_wave_size=4,
        )

        result = engine.generate(
            DiscoveryConfig(2, 1_000_000, 2000, 2000, ())
        )

        self.assertEqual(sampler.desired, 200)
        self.assertEqual(result["found"], 2)
        self.assertEqual(result["examined"], 4)
        self.assertEqual(resolver.calls, 4)
        self.assertEqual(resolver.max_active, 4)

    def test_generation_propagates_worker_errors(self):
        resolver = Mock()
        resolver.resolve.side_effect = RuntimeError("YouTube lookup failed")
        engine = DiscoveryEngine(
            sampler=FakeSampler([make_seed(0)]),
            resolver=resolver,
            resolver_workers=1,
            resolution_wave_size=1,
        )

        with self.assertRaisesRegex(RuntimeError, "YouTube lookup failed"):
            engine.generate(
                DiscoveryConfig(1, 0, 2000, 2000, ())
            )


class YouTubeMusicResolverTests(unittest.TestCase):
    def test_each_worker_reuses_its_own_client(self):
        created_clients = []
        create_lock = threading.Lock()
        worker_barrier = threading.Barrier(4)

        def client_factory():
            client = object()
            with create_lock:
                created_clients.append(client)
            return client

        resolver = YouTubeMusicResolver(client_factory=client_factory)

        def get_client_pair(_):
            worker_barrier.wait()
            return resolver._client(), resolver._client()

        with ThreadPoolExecutor(max_workers=4) as executor:
            pairs = list(executor.map(get_client_pair, range(4)))

        self.assertTrue(all(first is second for first, second in pairs))
        self.assertEqual(len({id(first) for first, _ in pairs}), 4)
        self.assertEqual(len(created_clients), 4)


if __name__ == "__main__":
    unittest.main()
