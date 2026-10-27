import importlib.util
import sys
import tempfile
import sqlite3
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mobile_announcer", ROOT / "announcer.py")
announcer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = announcer
SPEC.loader.exec_module(announcer)


class ParseMediaTests(unittest.TestCase):
    def test_movie_title_year_and_quality_fallback(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Oppenheimer.2023.2160p.BluRay.x265.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.media_type, "movie")
        self.assertEqual(info.title, "Oppenheimer")
        self.assertEqual(info.year, "2023")
        self.assertEqual(info.quality, "2160p / 4K")

    def test_guessit_parses_normal_release_names(self):
        if announcer.guessit is None:
            self.skipTest("guessit is an installed runtime dependency")
        movie = announcer.parse_media("Oppenheimer.2023.2160p.BluRay.x265.mkv")
        episode = announcer.parse_media("Loki.S02E03.720p.WEB-DL.HEVC.mkv")
        self.assertIsNotNone(movie)
        self.assertEqual(movie.title, "Oppenheimer")
        self.assertEqual(movie.media_type, "movie")
        self.assertEqual(movie.year, "2023")
        self.assertIsNotNone(episode)
        self.assertEqual(episode.title, "Loki")
        self.assertEqual(episode.media_type, "series")
        self.assertEqual((episode.season, episode.episode), (2, 3))

    def test_season_episode_detected_without_guessit(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Loki.S02E03.720p.WEB-DL.HEVC.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.media_type, "series")
        self.assertEqual(info.title, "Loki")
        self.assertEqual(info.season, 2)
        self.assertEqual(info.episode, 3)
        self.assertEqual(info.quality, "720p")

    def test_split_video_volume_is_supported(self):
        message = type("Message", (), {
            "video": None,
            "document": type("Document", (), {"mime_type": "application/octet-stream"})(),
        })()
        self.assertTrue(announcer._is_media_message(message, "Movie.2024.mkv.001"))

    def test_subtitles_are_not_announced_as_video(self):
        message = type("Message", (), {
            "video": None,
            "document": type("Document", (), {"mime_type": "application/x-subrip"})(),
        })()
        self.assertFalse(announcer._is_media_message(message, "Movie.2024.eng.srt"))

    def test_missing_filename_uses_caption_instead_of_synthetic_name(self):
        message = type("Message", (), {
            "id": 99,
            "video": type("Video", (), {"file_name": None, "mime_type": "video/mp4"})(),
            "document": None,
            "animation": None,
        })()
        self.assertEqual(announcer._message_file_name(message), "")
        info = announcer.parse_media("", "Dune Part Two 2024 1080p")
        self.assertIsNotNone(info)
        self.assertIn("Dune", info.title)


class AnnouncementFormattingTests(unittest.TestCase):
    def test_dedupe_title_level_ignores_quality(self):
        one = announcer.MediaInfo(title="Example Film", media_type="movie", year="2024", imdb_id="tt1234567", quality="720p")
        two = announcer.MediaInfo(title="Example Film", media_type="movie", year="2024", imdb_id="tt1234567", quality="2160p")
        self.assertEqual(announcer.dedupe_key(one), announcer.dedupe_key(two))

    def test_non_latin_titles_remain_distinct(self):
        first = announcer.MediaInfo(title="仙逆 Renegade Immortal", media_type="series", year="2023")
        second = announcer.MediaInfo(title="完美世界 Perfect World", media_type="series", year="2021")
        self.assertNotEqual(announcer.dedupe_key(first), announcer.dedupe_key(second))
        self.assertTrue(announcer._normalize_title(first.title))

    def test_episode_level_dedupe_is_optional(self):
        first = announcer.MediaInfo(title="Example Series", media_type="series", imdb_id="tt1234567", season=1, episode=1)
        second = announcer.MediaInfo(title="Example Series", media_type="series", imdb_id="tt1234567", season=1, episode=2)
        self.assertEqual(announcer.dedupe_key(first), announcer.dedupe_key(second))
        self.assertNotEqual(
            announcer.dedupe_key(first, announce_each_episode=True),
            announcer.dedupe_key(second, announce_each_episode=True),
        )

    def test_caption_escapes_html_and_stays_under_photo_caption_limit(self):
        info = announcer.MediaInfo(
            title="Film <script>",
            media_type="movie",
            description="A & B " * 300,
            rating="8.4",
            genres=["Sci-Fi", "Drama"],
            quality="1080p",
        )
        caption = announcer.build_caption(info)
        self.assertIn("&lt;script&gt;", caption)
        self.assertIn("A &amp; B", caption)
        self.assertLessEqual(len(caption), 900)

    def test_pathological_caption_never_truncates_html_markup(self):
        from html.parser import HTMLParser

        info = announcer.MediaInfo(
            title="<&" * 200,
            media_type="movie",
            description="<&" * 500,
            genres=["<&" * 40] * 4,
            quality="1080p",
        )
        caption = announcer.build_caption(info)
        self.assertLessEqual(len(caption), 900)

        class BalanceChecker(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.stack = []

            def handle_starttag(self, tag, attrs):
                if tag in {"b", "i"}:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in {"b", "i"}:
                    self.assert_tag_matches(tag)

            def assert_tag_matches(self, tag):
                if not self.stack or self.stack[-1] != tag:
                    raise AssertionError(f"Unbalanced HTML tag: {tag}")
                self.stack.pop()

        checker = BalanceChecker()
        checker.feed(caption)
        checker.close()
        self.assertEqual(checker.stack, [])

    def test_store_persists_dedupe_key_across_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            info = announcer.MediaInfo(title="Persistent Film", media_type="movie", year="2024")
            key = announcer.dedupe_key(info)
            store = announcer.AnnouncementStore(path)
            self.assertFalse(store.has(key))
            store.add(key, info, "-100123", 77, 88)

            reopened = announcer.AnnouncementStore(path)
            self.assertTrue(reopened.has(key))
            # INSERT OR IGNORE must not raise or create a second row.
            reopened.add(key, info, "-100123", 78, 89)
            with sqlite3.connect(path) as db:
                rows = db.execute("SELECT COUNT(*) FROM announced WHERE key = ?", (key,)).fetchone()[0]
            self.assertEqual(rows, 1)

    def test_http_redirect_base_is_not_used_for_app_buttons(self):
        info = announcer.MediaInfo(title="Film", media_type="movie", imdb_id="tt1234567")
        markup = announcer.build_keyboard(info, "http://127.0.0.1:8080")
        self.assertIsNotNone(markup)
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertTrue(all(button.url.startswith("https://") for button in buttons))
        self.assertIn("imdb.com/title/tt1234567", buttons[0].url)


class PosterFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_does_not_immediately_send_duplicate_text_post(self):
        worker = object.__new__(announcer.AnnouncementWorker)
        worker.config = SimpleNamespace(public_base_url="", announcement_channel="@test_channel")
        worker.client = SimpleNamespace(
            send_photo=AsyncMock(side_effect=OSError("connection timed out")),
            send_message=AsyncMock(return_value=object()),
        )
        info = announcer.MediaInfo(
            title="Poster Timeout Film",
            media_type="movie",
            poster="https://images.example.org/poster.jpg",
        )

        with self.assertRaises(OSError):
            await worker._send_announcement(info)

        worker.client.send_message.assert_not_awaited()

    async def test_telegram_api_rejection_falls_back_to_text_post(self):
        class FakeRPCError(Exception):
            pass

        worker = object.__new__(announcer.AnnouncementWorker)
        worker.config = SimpleNamespace(public_base_url="")
        sent_message = object()
        worker.client = SimpleNamespace(
            send_photo=AsyncMock(side_effect=FakeRPCError("invalid poster URL")),
            send_message=AsyncMock(return_value=sent_message),
        )
        info = announcer.MediaInfo(
            title="Fallback Film",
            media_type="movie",
            poster="https://images.example.org/poster.jpg",
        )

        with patch.object(announcer, "RPCError", FakeRPCError):
            result = await worker._send_announcement(info)

        self.assertIs(result, sent_message)
        worker.client.send_message.assert_awaited_once()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_longer_than_two_minutes_is_retried(self):
        class FakeFloodWait(Exception):
            def __init__(self, value):
                super().__init__(f"wait {value}s")
                self.value = value

        worker = object.__new__(announcer.AnnouncementWorker)
        worker._send_announcement = AsyncMock(side_effect=[FakeFloodWait(181), object()])
        info = announcer.MediaInfo(title="Retry Film", media_type="movie", year="2025")

        with patch.object(announcer, "FloodWait", FakeFloodWait), patch.object(
            announcer.asyncio, "sleep", new_callable=AsyncMock
        ) as sleep:
            result = await worker._send_announcement_with_retry(info)

        self.assertIsNotNone(result)
        self.assertEqual(worker._send_announcement.await_count, 2)
        sleep.assert_awaited_once_with(182)


if __name__ == "__main__":
    unittest.main(verbosity=2)
