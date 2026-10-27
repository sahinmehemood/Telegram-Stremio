import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mobile_announcer", ROOT / "announcer.py")
announcer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = announcer
SPEC.loader.exec_module(announcer)


def fake_message(*, chat_id=-100123, message_id=77, username="source", file_name=None,
                 mime_type="application/octet-stream", caption="", video=False):
    document = None
    if file_name is not None:
        document = SimpleNamespace(file_name=file_name, mime_type=mime_type)
    return SimpleNamespace(
        id=message_id,
        chat=SimpleNamespace(id=chat_id, username=username),
        video=SimpleNamespace(file_name=file_name) if video else None,
        document=document,
        animation=None,
        caption=caption,
        text="",
    )


class ParseMediaTests(unittest.TestCase):
    def test_requested_movie_filename_title_year_and_quality(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Dune.Part.Two.2024.2160p.WEB-DL.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.title, "Dune Part Two")
        self.assertEqual(info.year, "2024")
        self.assertEqual(info.media_type, "movie")
        self.assertEqual(info.quality, "2160p / 4K")

    def test_show_name_s02e05_is_series(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Show.Name.S02E05.1080p.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.title, "Show Name")
        self.assertEqual(info.media_type, "series")
        self.assertEqual((info.season, info.episode), (2, 5))
        self.assertEqual(info.quality, "1080p")

    def test_1x02_episode_pattern(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Show.1x02.720p.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.media_type, "series")
        self.assertEqual((info.season, info.episode), (1, 2))
        self.assertEqual(info.title, "Show")

    def test_lone_ep001_is_series(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("My.Show.EP001.480p.mkv")
        self.assertIsNotNone(info)
        self.assertEqual(info.media_type, "series")
        self.assertEqual(info.episode, 1)

    def test_guessit_handles_normal_release_names(self):
        if announcer.guessit is None:
            self.skipTest("guessit dependency is not installed")
        movie = announcer.parse_media("Oppenheimer.2023.2160p.BluRay.x265.mkv")
        episode = announcer.parse_media("Loki.S02E03.720p.WEB-DL.HEVC.mkv")
        self.assertIsNotNone(movie)
        self.assertEqual((movie.title, movie.year, movie.media_type), ("Oppenheimer", "2023", "movie"))
        self.assertIsNotNone(episode)
        self.assertEqual(episode.title, "Loki")
        self.assertEqual((episode.media_type, episode.season, episode.episode), ("series", 2, 3))

    def test_filename_then_caption_and_imdb_extraction(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("Dune.Part.Two.2024.1080p.mkv", "IMDb tt0137523")
            caption_only = announcer.parse_media("", "Dune Part Two 2024 1080p")
        self.assertEqual(info.imdb_id, "tt0137523")
        self.assertIsNotNone(caption_only)
        self.assertIn("Dune", caption_only.title)

    def test_title_with_only_release_tags_is_skipped(self):
        with patch.object(announcer, "guessit", None):
            info = announcer.parse_media("1080p.WEB-DL.x265.mkv")
        self.assertIsNone(info)

    def test_split_video_volume_is_supported(self):
        message = fake_message(file_name="Film.2024.mkv.001")
        self.assertTrue(announcer._is_media_message(message, "Film.2024.mkv.001"))

    def test_video_mime_is_supported(self):
        message = fake_message(file_name="upload.bin", mime_type="video/mp4")
        self.assertTrue(announcer._is_media_message(message, "upload.bin"))

    def test_text_photos_audio_and_subtitles_are_not_video(self):
        text_message = fake_message(file_name=None)
        audio_message = fake_message(file_name="sound.mp3", mime_type="audio/mpeg")
        subtitle_message = fake_message(file_name="Film.eng.srt", mime_type="application/x-subrip")
        self.assertFalse(announcer._is_media_message(text_message, ""))
        self.assertFalse(announcer._is_media_message(audio_message, "sound.mp3"))
        self.assertFalse(announcer._is_media_message(subtitle_message, "Film.eng.srt"))


class DedupeTests(unittest.TestCase):
    def test_quality_does_not_change_title_key(self):
        one = announcer.MediaInfo(title="Dune Part Two", media_type="movie", year="2024",
                                  imdb_id="tt15239678", quality="720p")
        two = announcer.MediaInfo(title="Dune Part Two", media_type="movie", year="2024",
                                  imdb_id="tt15239678", quality="2160p")
        self.assertEqual(announcer.dedupe_key(one), "imdb:tt15239678")
        self.assertEqual(announcer.dedupe_key(one), announcer.dedupe_key(two))

    def test_episode_dedupe_is_optional_for_same_imdb_id(self):
        first = announcer.MediaInfo(title="Example Show", media_type="series",
                                    imdb_id="tt1234567", season=2, episode=5)
        second = announcer.MediaInfo(title="Example Show", media_type="series",
                                     imdb_id="tt1234567", season=2, episode=6)
        self.assertEqual(announcer.dedupe_key(first), announcer.dedupe_key(second))
        self.assertNotEqual(
            announcer.dedupe_key(first, announce_each_episode=True),
            announcer.dedupe_key(second, announce_each_episode=True),
        )
        self.assertEqual(
            announcer.dedupe_key(first, announce_each_episode=True),
            "imdb:tt1234567:s2e5",
        )

    def test_bengali_and_japanese_titles_do_not_collapse(self):
        bengali_one = announcer.MediaInfo(title="অপরাজিত", media_type="movie", year="2022")
        bengali_two = announcer.MediaInfo(title="পথের পাঁচালী", media_type="movie", year="1955")
        japanese_one = announcer.MediaInfo(title="君の名は", media_type="movie", year="2016")
        japanese_two = announcer.MediaInfo(title="千と千尋の神隠し", media_type="movie", year="2001")
        self.assertNotEqual(announcer.dedupe_key(bengali_one), announcer.dedupe_key(bengali_two))
        self.assertNotEqual(announcer.dedupe_key(japanese_one), announcer.dedupe_key(japanese_two))
        self.assertTrue(announcer._normalize_title("পথের পাঁচালী"))
        self.assertTrue(announcer._normalize_title("君の名は"))

    def test_missing_year_uses_na(self):
        info = announcer.MediaInfo(title="Unknown Year Film", media_type="movie")
        self.assertEqual(announcer.dedupe_key(info), "title:unknownyearfilm:na")


class CaptionAndButtonTests(unittest.TestCase):
    def test_caption_matches_layout_and_escapes_html(self):
        info = announcer.MediaInfo(
            title="Film <script>",
            media_type="movie",
            year="2024",
            rating="8.4",
            genres=["Sci-Fi", "Drama", "Action", "Mystery", "Unused"],
            quality="1080p",
            description="A & B",
        )
        caption = announcer.build_caption(info)
        self.assertTrue(caption.startswith("🎬 <b>Film &lt;script&gt;</b> (2024)"))
        self.assertIn("🗂 <b>Type:</b> Movie", caption)
        self.assertIn("⭐ <b>Rating:</b> 8.4", caption)
        self.assertIn("🎭 <b>Genres:</b> Sci-Fi, Drama, Action, Mystery", caption)
        self.assertNotIn("Unused", caption)
        self.assertIn("📶 <b>Quality:</b> 1080p", caption)
        self.assertIn("<i>A &amp; B</i>", caption)
        self.assertNotIn("8.4/10", caption)

    def test_description_over_320_chars_is_317_plus_ellipsis(self):
        description = "x" * 400
        caption = announcer.build_caption(
            announcer.MediaInfo(title="Film", media_type="movie", description=description)
        )
        self.assertIn("<i>" + ("x" * 317) + "...</i>", caption)
        self.assertNotIn("x" * 318, caption)

    def test_public_https_base_creates_exact_movie_buttons(self):
        info = announcer.MediaInfo(title="Fight Club", media_type="movie", imdb_id="tt0137523")
        markup = announcer.build_keyboard(info, "https://example.com")
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["▶️ Stremio", "📱 Nuvio"])
        self.assertEqual(buttons[0].url, "https://example.com/open/stremio/movie/tt0137523")
        self.assertEqual(buttons[1].url, "https://example.com/open/nuvio/movie/tt0137523")

    def test_series_public_https_base_uses_series_path(self):
        info = announcer.MediaInfo(title="Example Show", media_type="series", imdb_id="tt1234567")
        markup = announcer.build_keyboard(info, "https://example.com/")
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(buttons[0].url, "https://example.com/open/stremio/series/tt1234567")
        self.assertEqual(buttons[1].url, "https://example.com/open/nuvio/series/tt1234567")

    def test_local_or_http_base_falls_back_to_imdb_button(self):
        info = announcer.MediaInfo(title="Fight Club", media_type="movie", imdb_id="tt0137523")
        for base in (
            "http://127.0.0.1:8080",
            "https://localhost:8443",
            "https://10.0.0.4",
            "https://192.168.1.4",
            "https://172.16.2.3",
        ):
            with self.subTest(base=base):
                markup = announcer.build_keyboard(info, base)
                buttons = [button for row in markup.inline_keyboard for button in row]
                self.assertEqual(len(buttons), 1)
                self.assertEqual(buttons[0].text, "IMDb")
                self.assertEqual(buttons[0].url, "https://www.imdb.com/title/tt0137523/")

    def test_without_imdb_uses_tmdb_link_only_when_available(self):
        no_id = announcer.MediaInfo(title="Unknown", media_type="movie")
        self.assertIsNone(announcer.build_keyboard(no_id, ""))
        with_id = announcer.MediaInfo(title="Unknown", media_type="series", tmdb_id="1234")
        markup = announcer.build_keyboard(with_id, "")
        self.assertEqual(markup.inline_keyboard[0][0].text, "TMDb")
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://www.themoviedb.org/tv/1234")


class StoreTests(unittest.TestCase):
    def test_store_persists_key_and_deletion_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            info = announcer.MediaInfo(title="Persistent Film", media_type="movie", year="2024")
            key = announcer.dedupe_key(info)
            store = announcer.AnnouncementStore(path)
            self.assertFalse(store.has(key))
            store.add(key, info, -100123, 77, -100555, 900)

            reopened = announcer.AnnouncementStore(path)
            self.assertTrue(reopened.has(key))
            rows = reopened.find_source_message(-100123, 77)
            self.assertEqual(rows, [{"key": key, "sent_chat_id": "-100555", "sent_message_id": 900}])
            reopened.add(key, info, -100123, 78, -100555, 901)
            with sqlite3.connect(path) as db:
                count = db.execute("SELECT COUNT(*) FROM announced WHERE key = ?", (key,)).fetchone()[0]
                columns = {row[1] for row in db.execute("PRAGMA table_info(announced)")}
            self.assertEqual(count, 1)
            self.assertTrue({"source_chat_id", "source_message_id", "sent_chat_id", "sent_message_id"} <= columns)


class AsyncWorkerTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, temporary, info, client):
        worker = object.__new__(announcer.AnnouncementWorker)
        worker.client = client
        worker.config = SimpleNamespace(
            state_db=Path(temporary) / "state.sqlite3",
            source_ids={-100123},
            source_usernames=set(),
            announcement_channel=-100555,
            public_base_url="",
            announce_each_episode=False,
        )
        worker.source_chat_ids = {-100123}
        worker.store = announcer.AnnouncementStore(worker.config.state_db)
        worker.metadata = SimpleNamespace(enrich=AsyncMock(side_effect=lambda parsed: info))
        worker._lock = asyncio.Lock()
        return worker

    async def test_photo_exception_falls_back_to_text_and_stores_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            info = announcer.MediaInfo(title="Poster Film", media_type="movie", poster="https://image.example/poster.jpg")
            sent = SimpleNamespace(id=900, chat=SimpleNamespace(id=-100555))
            client = SimpleNamespace(
                send_photo=AsyncMock(side_effect=OSError("poster host rejected URL")),
                send_message=AsyncMock(return_value=sent),
                delete_messages=AsyncMock(),
            )
            worker = self._worker(temporary, info, client)
            message = fake_message(file_name="Poster.Film.2024.mkv", message_id=77)

            await worker.handle(client, message)

            client.send_photo.assert_awaited_once()
            client.send_message.assert_awaited_once()
            self.assertTrue(worker.store.has(announcer.dedupe_key(info)))
            with sqlite3.connect(worker.config.state_db) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM announced").fetchone()[0], 1)

    async def test_failed_text_send_stores_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            info = announcer.MediaInfo(title="Text Failure Film", media_type="movie")
            client = SimpleNamespace(
                send_photo=AsyncMock(),
                send_message=AsyncMock(side_effect=RuntimeError("channel rejects post")),
                delete_messages=AsyncMock(),
            )
            worker = self._worker(temporary, info, client)
            message = fake_message(file_name="Text.Failure.Film.2024.mkv")

            await worker.handle(client, message)

            client.send_message.assert_awaited_once()
            self.assertFalse(worker.store.has(announcer.dedupe_key(info)))

    async def test_deleted_source_message_deletes_announcement_and_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            info = announcer.MediaInfo(title="Deleted Film", media_type="movie", year="2024")
            client = SimpleNamespace(
                delete_messages=AsyncMock(return_value=True),
                send_photo=AsyncMock(),
                send_message=AsyncMock(),
            )
            worker = self._worker(temporary, info, client)
            key = announcer.dedupe_key(info)
            worker.store.add(key, info, -100123, 77, -100555, 900)

            await worker.handle_deleted(client, [SimpleNamespace(chat=SimpleNamespace(id=-100123), id=77)])

            client.delete_messages.assert_awaited_once_with(chat_id="-100555", message_ids=[900])
            self.assertEqual(worker.store.find_source_message(-100123, 77), [])
            self.assertFalse(worker.store.has(key))

    async def test_forbidden_delete_still_removes_dedupe_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            info = announcer.MediaInfo(title="Forbidden Delete", media_type="movie", year="2024")
            client = SimpleNamespace(delete_messages=AsyncMock(side_effect=RuntimeError("forbidden")))
            worker = self._worker(temporary, info, client)
            key = announcer.dedupe_key(info)
            worker.store.add(key, info, -100123, 77, -100555, 900)

            await worker.handle_deleted(client, [SimpleNamespace(chat=SimpleNamespace(id=-100123), id=77)])

            self.assertFalse(worker.store.has(key))

    async def test_flood_wait_sleeps_then_retries_exactly_once(self):
        class FakeFloodWait(Exception):
            def __init__(self, seconds):
                self.value = seconds

        with tempfile.TemporaryDirectory() as temporary:
            info = announcer.MediaInfo(title="Flood Wait Film", media_type="movie")
            worker = object.__new__(announcer.AnnouncementWorker)
            worker._send_announcement = AsyncMock(side_effect=[FakeFloodWait(17), object()])
            with patch.object(announcer, "FloodWait", FakeFloodWait), patch.object(
                announcer.asyncio, "sleep", new_callable=AsyncMock
            ) as sleep:
                result = await worker._send_announcement_with_retry(info)
            self.assertIsNotNone(result)
            self.assertEqual(worker._send_announcement.await_count, 2)
            sleep.assert_awaited_once_with(17)

    async def test_explicit_imdb_id_does_not_trigger_fuzzy_search(self):
        metadata = object.__new__(announcer.Cinemeta)
        metadata._cache = {}
        metadata._get_meta = AsyncMock(return_value=None)
        metadata._search = AsyncMock(return_value=[{"id": "tt7654321", "name": "Different title"}])
        info = announcer.MediaInfo(title="User Supplied", media_type="movie", imdb_id="tt1234567")

        await metadata.enrich(info)

        metadata._get_meta.assert_awaited_once_with("movie", "tt1234567")
        metadata._search.assert_not_awaited()
        self.assertEqual(info.imdb_id, "tt1234567")


if __name__ == "__main__":
    unittest.main(verbosity=2)
