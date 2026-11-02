import importlib.util
from pathlib import Path
import unittest


# Load this pure helper directly: importing Backend.helper normally executes
# Backend/__init__.py, which constructs the MongoDB Database singleton and
# intentionally requires production DATABASE configuration.
MODULE_PATH = Path(__file__).resolve().parents[1] / "Backend" / "helper" / "video_filter.py"
SPEC = importlib.util.spec_from_file_location("video_filter_under_test", MODULE_PATH)
video_filter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(video_filter)
is_below_minimum_video_size = video_filter.is_below_minimum_video_size
normalize_minimum_video_size_enabled = video_filter.normalize_minimum_video_size_enabled
normalize_minimum_video_size_mb = video_filter.normalize_minimum_video_size_mb
is_video_file = video_filter.is_video_file


class MinimumVideoSizeFilterTests(unittest.TestCase):
    def test_skips_video_below_threshold(self):
        self.assertTrue(is_below_minimum_video_size(49 * 1024 * 1024, 50))

    def test_keeps_video_exactly_at_threshold(self):
        self.assertFalse(is_below_minimum_video_size(50 * 1024 * 1024, 50))

    def test_keeps_video_above_threshold(self):
        self.assertFalse(is_below_minimum_video_size(51 * 1024 * 1024, 50))

    def test_disabled_filter_keeps_small_video(self):
        self.assertFalse(is_below_minimum_video_size(1, 50, enabled=False))

    def test_unknown_size_is_not_dropped(self):
        self.assertFalse(is_below_minimum_video_size(None, 50))

    def test_zero_threshold_disables_size_based_skipping(self):
        self.assertFalse(is_below_minimum_video_size(1, 0))

    def test_negative_or_invalid_values_fail_open(self):
        self.assertFalse(is_below_minimum_video_size(-1, 50))
        self.assertFalse(is_below_minimum_video_size("unknown", 50))

    def test_boolean_setting_accepts_only_boolean_or_true_false_strings(self):
        self.assertTrue(normalize_minimum_video_size_enabled(True))
        self.assertFalse(normalize_minimum_video_size_enabled(False))
        self.assertTrue(normalize_minimum_video_size_enabled("TRUE"))
        self.assertFalse(normalize_minimum_video_size_enabled("false"))
        for value in ("yes", "0", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_minimum_video_size_enabled(value)

    def test_video_document_detection_uses_mime_or_extension(self):
        self.assertTrue(is_video_file("video/mp4", "clip.bin"))
        self.assertTrue(is_video_file("application/octet-stream", "movie.mkv"))
        self.assertTrue(is_video_file(None, "movie.MP4"))
        self.assertFalse(is_video_file("application/zip", "archive.rar"))
        self.assertFalse(is_video_file(None, "poster.jpg"))

    def test_threshold_accepts_whole_numbers_only(self):
        self.assertEqual(normalize_minimum_video_size_mb(50), 50)
        self.assertEqual(normalize_minimum_video_size_mb(50.0), 50)
        self.assertEqual(normalize_minimum_video_size_mb("50"), 50)
        for value in (True, 50.5, "50.5", None, -1, 102401):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_minimum_video_size_mb(value)


if __name__ == "__main__":
    unittest.main()
