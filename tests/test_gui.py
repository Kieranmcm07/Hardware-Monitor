import unittest

from hardware_monitor.gui import compact_volume_name, mix_color


class GuiHelperTests(unittest.TestCase):
    def test_mix_color_preserves_endpoints(self):
        self.assertEqual(mix_color("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(mix_color("#000000", "#ffffff", 1), "#ffffff")

    def test_mix_color_clamps_and_blends(self):
        self.assertEqual(mix_color("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(mix_color("#112233", "#ffffff", -5), "#112233")
        self.assertEqual(mix_color("#112233", "#ffffff", 5), "#ffffff")

    def test_compact_volume_name_keeps_both_ends(self):
        name = "/media/kieran/Very Long External Storage Volume"
        result = compact_volume_name(name, 24)
        self.assertLessEqual(len(result), 24)
        self.assertTrue(result.startswith(name[:8]))
        self.assertTrue(result.endswith(name[-8:]))
        self.assertIn("…", result)


if __name__ == "__main__":
    unittest.main()
