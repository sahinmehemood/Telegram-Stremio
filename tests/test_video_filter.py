import unittest

from Backend.helper.video_filter import is_below_minimum_video_size


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


if __name__ == "__main__":
    unittest.main()
