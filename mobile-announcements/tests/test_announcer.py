import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mobile_announcer", ROOT / "announcer.py")
announcer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
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

    def test_http_redirect_base_is_not_used_for_app_buttons(self):
        info = announcer.MediaInfo(title="Film", media_type="movie", imdb_id="tt1234567")
        markup = announcer.build_keyboard(info, "http://127.0.0.1:8080")
        self.assertIsNotNone(markup)
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertTrue(all(button.url.startswith("https://") for button in buttons))
        self.assertIn("imdb.com/title/tt1234567", buttons[0].url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
