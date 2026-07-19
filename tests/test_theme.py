import unittest

try:
    from hardware_monitor.theme import (
        THEME_PRESETS,
        contrast_ratio,
        move_metric,
        normalize_hex,
        normalize_metric_order,
        resolve_theme,
        set_metric_enabled,
        toggle_metric,
        validate_accent,
    )
except ModuleNotFoundError:
    from v4_theme import (
        THEME_PRESETS,
        contrast_ratio,
        move_metric,
        normalize_hex,
        normalize_metric_order,
        resolve_theme,
        set_metric_enabled,
        toggle_metric,
        validate_accent,
    )


class ThemeTests(unittest.TestCase):
    def test_colour_validation_and_aliases(self):
        self.assertEqual(normalize_hex(" F34 "), "#ff3344")
        self.assertEqual(normalize_hex("#A0b1C2"), "#a0b1c2")
        self.assertIsNone(normalize_hex("transparent"))
        self.assertEqual(validate_accent("red"), "#f23d52")
        self.assertEqual(validate_accent("ruby"), "#e21849")
        self.assertEqual(validate_accent("#030304"), "#f23d52")
        self.assertEqual(validate_accent("broken"), "#f23d52")

    def test_presets_have_readable_text_and_visible_accent(self):
        self.assertGreaterEqual(len(THEME_PRESETS), 3)
        for theme in THEME_PRESETS.values():
            with self.subTest(theme=theme.key):
                self.assertGreaterEqual(contrast_ratio(theme.text, theme.surface), 4.5)
                self.assertGreaterEqual(contrast_ratio(theme.accent, theme.background), 3.0)

    def test_resolution_is_immutable_and_custom_accent_is_derived(self):
        original = resolve_theme("pitch_black")
        changed = resolve_theme("pitch_black", "#ff5068")
        self.assertEqual(changed.accent, "#ff5068")
        self.assertNotEqual(changed.accent_hover, changed.accent)
        self.assertEqual(original.accent, "#ff334f")
        self.assertEqual(resolve_theme("unknown").key, "graphite")


class DashboardLayoutTests(unittest.TestCase):
    def test_normalization_preserves_valid_user_order(self):
        self.assertEqual(
            normalize_metric_order(["disk", "cpu", "CPU", "ram", "bad", "temp"]),
            ("storage", "cpu", "memory", "temperature"),
        )
        self.assertEqual(normalize_metric_order(["bad"]), ("cpu", "memory", "storage"))
        self.assertEqual(normalize_metric_order([], allow_empty=True), ())

    def test_toggle_enable_and_minimum_are_immutable(self):
        source = ["cpu", "memory"]
        self.assertEqual(toggle_metric(source, "network"), ("cpu", "memory", "network"))
        self.assertEqual(source, ["cpu", "memory"])
        self.assertEqual(set_metric_enabled(("cpu",), "cpu", False), ("cpu",))
        self.assertEqual(
            set_metric_enabled(("cpu",), "cpu", False, minimum=0), ()
        )

    def test_move_clamps_and_rejects_disabled_metric(self):
        order = ("cpu", "memory", "storage")
        self.assertEqual(move_metric(order, "storage", -1), ("cpu", "storage", "memory"))
        self.assertEqual(move_metric(order, "cpu", 99), ("memory", "storage", "cpu"))
        with self.assertRaises(ValueError):
            move_metric(order, "network", 1)


if __name__ == "__main__":
    unittest.main()
