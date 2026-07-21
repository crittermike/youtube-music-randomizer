#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from ytmusicapi import YTMusic


APP_DIR = Path(__file__).resolve().parent
INDEX_PATH = APP_DIR / "index.html"
JELLY_PATH = APP_DIR / "vendor" / "jelly.js"
APP_NAME = "YouTube Music Randomizer"
APP_VERSION = "0.1.0"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_USER_AGENT = (
    f"YouTubeMusicRandomizer/{APP_VERSION} "
    "(https://github.com/crittermike/youtube-music-randomizer)"
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_BODY_BYTES = 64 * 1024
CURRENT_YEAR = datetime.now().year


class DiscoveryError(RuntimeError):
    pass


class CatalogUnavailable(DiscoveryError):
    pass


@dataclass(frozen=True)
class DiscoveryConfig:
    count: int
    min_views: int
    oldest_year: int
    newest_year: int
    avoid_terms: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DiscoveryConfig":
        count = _bounded_int(payload, "count", 15, 5, 30)
        min_views = _bounded_int(
            payload, "minViews", 10_000, 0, 10_000_000_000
        )
        oldest_year = _bounded_int(payload, "oldestYear", 1950, 1900, CURRENT_YEAR)
        newest_year = _bounded_int(
            payload, "newestYear", CURRENT_YEAR, 1900, CURRENT_YEAR
        )
        if oldest_year > newest_year:
            raise ValueError("The oldest year must not be later than the newest year.")

        raw_avoid = payload.get("avoidTerms", "")
        if not isinstance(raw_avoid, str):
            raise ValueError("Avoid terms must be text.")
        if len(raw_avoid) > 500:
            raise ValueError("Avoid terms must be 500 characters or fewer.")
        avoid_terms = tuple(
            term
            for term in {
                normalize_text(part)
                for part in re.split(r"[,\n]", raw_avoid)
            }
            if term
        )

        return cls(
            count=count,
            min_views=min_views,
            oldest_year=oldest_year,
            newest_year=newest_year,
            avoid_terms=avoid_terms,
        )


@dataclass(frozen=True)
class RecordingSeed:
    mbid: str
    title: str
    artist: str
    year: str

    @classmethod
    def from_musicbrainz(cls, raw: dict[str, Any]) -> "RecordingSeed | None":
        title = str(raw.get("title") or "").strip()
        credits = raw.get("artist-credit") or []
        artist = "".join(
            f"{credit.get('name', '')}{credit.get('joinphrase', '')}"
            for credit in credits
            if isinstance(credit, dict)
        ).strip()
        mbid = str(raw.get("id") or "").strip()
        year = str(raw.get("first-release-date") or "").strip()[:4]
        if not title or not artist or not mbid:
            return None
        if normalize_text(artist) in {"unknown", "various artists"}:
            return None
        return cls(mbid=mbid, title=title, artist=artist, year=year)


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    artist: str
    album: str
    duration_seconds: int
    views: int
    thumbnail: str
    source_title: str
    source_artist: str
    source_year: str
    musicbrainz_id: str
    match_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "videoId": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "durationSeconds": self.duration_seconds,
            "views": self.views,
            "thumbnail": self.thumbnail,
            "sourceTitle": self.source_title,
            "sourceArtist": self.source_artist,
            "sourceYear": self.source_year,
            "musicbrainzId": self.musicbrainz_id,
            "matchScore": round(self.match_score, 3),
            "url": f"https://music.youtube.com/watch?v={self.video_id}",
        }


def _bounded_int(
    payload: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a number.")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def text_similarity(expected: str, actual: str) -> float:
    expected_normalized = normalize_text(expected)
    actual_normalized = normalize_text(actual)
    if not expected_normalized or not actual_normalized:
        return 0.0

    sequence_score = SequenceMatcher(
        None, expected_normalized, actual_normalized
    ).ratio()
    expected_tokens = set(expected_normalized.split())
    actual_tokens = set(actual_normalized.split())
    containment_score = len(expected_tokens & actual_tokens) / max(
        1, len(expected_tokens)
    )
    return max(sequence_score, containment_score)


def recording_match_score(
    expected_title: str,
    expected_artist: str,
    actual_title: str,
    actual_artist: str,
) -> tuple[float, float, float]:
    title_score = text_similarity(expected_title, actual_title)
    artist_score = text_similarity(expected_artist, actual_artist)
    combined = (title_score * 0.7) + (artist_score * 0.3)
    return combined, title_score, artist_score


def parse_views(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = str(value).strip().upper().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(number * multiplier[match.group(2)])


def contains_avoided_term(values: Iterable[str], avoid_terms: tuple[str, ...]) -> bool:
    combined = normalize_text(" ".join(values))
    return any(term in combined for term in avoid_terms)


class MusicBrainzSampler:
    def __init__(self) -> None:
        self._last_request_at = 0.0
        self._random = random.SystemRandom()

    def sample(
        self,
        desired: int,
        oldest_year: int,
        newest_year: int,
    ) -> tuple[list[RecordingSeed], list[int]]:
        page_size = 40
        page_count = min(9, max(4, math.ceil(desired / page_size)))
        years = self._stratified_years(page_count, oldest_year, newest_year)
        seeds: list[RecordingSeed] = []
        seen_ids: set[str] = set()

        for year in years:
            offset = self._random.randrange(0, 1_201, page_size)
            data = self._request_page(year, offset, page_size)
            recordings = data.get("recordings")
            if not isinstance(recordings, list):
                raise CatalogUnavailable(
                    "MusicBrainz returned an unexpected response."
                )
            self._random.shuffle(recordings)
            for raw in recordings:
                if not isinstance(raw, dict):
                    continue
                seed = RecordingSeed.from_musicbrainz(raw)
                if seed is None or seed.mbid in seen_ids:
                    continue
                seen_ids.add(seed.mbid)
                seeds.append(seed)

        self._random.shuffle(seeds)
        return seeds[:desired], years

    def _stratified_years(
        self, count: int, oldest_year: int, newest_year: int
    ) -> list[int]:
        span = newest_year - oldest_year + 1
        if count >= span:
            years = list(range(oldest_year, newest_year + 1))
            self._random.shuffle(years)
            return years[:count]

        bucket_width = span / count
        years = []
        for index in range(count):
            bucket_start = oldest_year + math.floor(index * bucket_width)
            bucket_end = oldest_year + math.floor((index + 1) * bucket_width) - 1
            bucket_end = min(newest_year, max(bucket_start, bucket_end))
            years.append(self._random.randint(bucket_start, bucket_end))
        self._random.shuffle(years)
        return years

    def _request_page(self, year: int, offset: int, limit: int) -> dict[str, Any]:
        params = urlencode(
            {
                "query": f"firstreleasedate:{year} AND recording:*",
                "fmt": "json",
                "limit": limit,
                "offset": offset,
            }
        )
        request = Request(
            f"{MUSICBRAINZ_URL}?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": MUSICBRAINZ_USER_AGENT,
            },
        )

        last_error: Exception | None = None
        for attempt in range(3):
            wait_seconds = max(0.0, 1.1 - (time.monotonic() - self._last_request_at))
            if wait_seconds:
                time.sleep(wait_seconds)
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=20) as response:
                    raw = response.read(5 * 1024 * 1024)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise CatalogUnavailable(
                        "MusicBrainz returned an unexpected response."
                    )
                return data
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {
                    HTTPStatus.TOO_MANY_REQUESTS,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                }:
                    break
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(1.5 * (attempt + 1))

        raise CatalogUnavailable(
            "MusicBrainz is temporarily unavailable. Try generating again shortly."
        ) from last_error


class YouTubeMusicResolver:
    def __init__(self) -> None:
        self.client = YTMusic()

    def resolve(
        self,
        seed: RecordingSeed,
        min_views: int,
        avoid_terms: tuple[str, ...],
    ) -> Track | None:
        if contains_avoided_term((seed.title, seed.artist), avoid_terms):
            return None

        results = self.client.search(
            f"{seed.artist} {seed.title}",
            filter="songs",
            limit=8,
            ignore_spelling=True,
        )
        ranked: list[tuple[float, dict[str, Any], str]] = []

        for result in results:
            if not isinstance(result, dict) or result.get("resultType") != "song":
                continue
            video_id = str(result.get("videoId") or "")
            if not VIDEO_ID_RE.fullmatch(video_id):
                continue

            title = str(result.get("title") or "").strip()
            artists = result.get("artists") or []
            artist = ", ".join(
                str(item.get("name") or "").strip()
                for item in artists
                if isinstance(item, dict) and item.get("name")
            )
            if not title or not artist:
                continue
            if contains_avoided_term((title, artist), avoid_terms):
                continue

            combined, title_score, artist_score = recording_match_score(
                seed.title, seed.artist, title, artist
            )
            if combined < 0.67 or title_score < 0.66 or artist_score < 0.45:
                continue
            ranked.append((combined, result, artist))

        ranked.sort(key=lambda item: item[0], reverse=True)
        for combined, result, artist in ranked[:4]:
            approximate_views = parse_views(result.get("views"))
            if approximate_views is not None and approximate_views < min_views:
                continue

            song = self.client.get_song(str(result["videoId"]))
            details = song.get("videoDetails") or {}
            playability = song.get("playabilityStatus") or {}
            if playability.get("status") != "OK":
                continue
            if details.get("isLiveContent"):
                continue

            views = parse_views(details.get("viewCount"))
            duration = parse_views(details.get("lengthSeconds"))
            if views is None or views < min_views:
                continue
            if duration is None or not 60 <= duration <= 900:
                continue

            thumbnails = result.get("thumbnails") or []
            thumbnail = ""
            if thumbnails and isinstance(thumbnails[-1], dict):
                thumbnail = str(thumbnails[-1].get("url") or "")
            album_data = result.get("album")
            album = (
                str(album_data.get("name") or "")
                if isinstance(album_data, dict)
                else ""
            )

            return Track(
                video_id=str(result["videoId"]),
                title=str(result.get("title") or seed.title),
                artist=artist,
                album=album,
                duration_seconds=duration,
                views=views,
                thumbnail=thumbnail,
                source_title=seed.title,
                source_artist=seed.artist,
                source_year=seed.year,
                musicbrainz_id=seed.mbid,
                match_score=combined,
            )

        return None


class DiscoveryEngine:
    def generate(self, config: DiscoveryConfig) -> dict[str, Any]:
        desired_seed_count = max(100, config.count * 14)
        sampler = MusicBrainzSampler()
        seeds, sampled_years = sampler.sample(
            desired_seed_count,
            config.oldest_year,
            config.newest_year,
        )
        if not seeds:
            raise CatalogUnavailable(
                "MusicBrainz did not return recordings for that year range."
            )

        resolver = YouTubeMusicResolver()
        tracks: list[Track] = []
        seen_video_ids: set[str] = set()
        seen_artists: set[str] = set()
        examined = 0

        for seed in seeds:
            if len(tracks) >= config.count:
                break
            examined += 1
            track = resolver.resolve(seed, config.min_views, config.avoid_terms)
            if track is None or track.video_id in seen_video_ids:
                continue
            artist_key = normalize_text(track.artist)
            if artist_key in seen_artists:
                continue
            seen_video_ids.add(track.video_id)
            seen_artists.add(artist_key)
            tracks.append(track)

        random.SystemRandom().shuffle(tracks)
        return {
            "tracks": [track.to_dict() for track in tracks],
            "requested": config.count,
            "found": len(tracks),
            "examined": examined,
            "sampledYears": sorted(sampled_years),
            "minViews": config.min_views,
        }


class AppHandler(BaseHTTPRequestHandler):
    server_version = f"YouTubeMusicRandomizer/{APP_VERSION}"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
            return
        if path == "/healthz":
            self._send_json({"status": "ok"})
            return
        if path == "/vendor/jelly.js":
            self._send_asset(JELLY_PATH, "application/javascript; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._origin_is_allowed():
            self._send_json({"error": "Forbidden origin."}, HTTPStatus.FORBIDDEN)
            return

        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/generate":
                config = DiscoveryConfig.from_payload(payload)
                result = DiscoveryEngine().generate(config)
                self._send_json(result)
                return
            self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except DiscoveryError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except json.JSONDecodeError:
            self._send_json(
                {"error": "The request body must be valid JSON."},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            print(f"Request failed: {exc!r}")
            self._send_json(
                {"error": "The request failed unexpectedly. Check the terminal."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        # Same-origin: the browser Origin host matches the Host we served from.
        # This covers HTTPS deployments (e.g. Fly) without hardcoding the domain.
        host_header = self.headers.get("Host", "")
        if parsed.netloc and parsed.netloc == host_header:
            return True
        return parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
        }

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid request length.") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("The request body is too large.")
        raw = self.rfile.read(length)
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object.")
        return payload

    def _send_html(self) -> None:
        try:
            body = INDEX_PATH.read_bytes()
        except OSError as exc:
            print(f"Could not read {INDEX_PATH}: {exc}")
            self._send_json(
                {"error": "The app UI is missing."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://*.googleusercontent.com "
            "https://i.ytimg.com; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, file_path: Path, content_type: str) -> None:
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(format_string, *args)


def run_server(host: str, port: int, open_browser: bool) -> None:
    if not INDEX_PATH.exists():
        raise SystemExit(f"Missing UI file: {INDEX_PATH}")
    address = (host, port)
    server = ThreadingHTTPServer(address, AppHandler)
    display_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    url = f"http://{display_host}:{server.server_port}"
    print(f"{APP_NAME} is running at {url}")
    print("Press Control-C to stop it.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopping {APP_NAME}.")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate random YouTube Music discoveries."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Start the local web app.")
    serve_parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Interface to bind (default 127.0.0.1, or $HOST).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8787")),
        help="Port to listen on (default 8787, or $PORT).",
    )
    serve_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the app automatically.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.port <= 65_535:
        raise SystemExit("Port must be between 1 and 65535.")
    run_server(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
