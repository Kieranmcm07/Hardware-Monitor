import unittest

try:
    from hardware_monitor.control_center import (
        FEATURES,
        FeatureSpec,
        FeatureStatus,
        LabNavigationState,
        feature_by_key,
        feature_rows,
        filter_features,
        group_counts,
        normalize_feature_key,
        normalize_status,
        normalize_statuses,
        responsive_columns,
        status_badge,
    )
except ModuleNotFoundError:
    from v4_control_center import (
        FEATURES,
        FeatureSpec,
        FeatureStatus,
        LabNavigationState,
        feature_by_key,
        feature_rows,
        filter_features,
        group_counts,
        normalize_feature_key,
        normalize_status,
        normalize_statuses,
        responsive_columns,
        status_badge,
    )


class FeatureCatalogTests(unittest.TestCase):
    def test_catalog_has_every_promised_lab_area_once(self):
        keys = [feature.key for feature in FEATURES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            set(keys),
            {
                "processes", "alerts", "sensors", "history", "diagnostics",
                "benchmarks", "reports", "customization",
            },
        )
        self.assertIn("smart", feature_by_key("sensors").search_text)

    def test_aliases_resolve_and_unknowns_raise(self):
        self.assertEqual(normalize_feature_key("Drive Health"), "sensors")
        self.assertEqual(feature_by_key("network-diagnostics").key, "diagnostics")
        self.assertEqual(feature_by_key("settings").key, "customization")
        with self.assertRaises(KeyError):
            feature_by_key("missing")

    def test_search_uses_all_tokens_and_group_filter(self):
        self.assertEqual(
            tuple(feature.key for feature in filter_features(query="cpu apps")),
            ("processes",),
        )
        self.assertEqual(
            tuple(feature.key for feature in filter_features(group="tools")),
            ("diagnostics", "benchmarks", "reports"),
        )
        self.assertEqual(filter_features(query="no such feature"), ())

    def test_unavailable_features_can_be_hidden(self):
        statuses = {"sensors": {"state": "unavailable", "detail": "No provider"}}
        visible = filter_features(statuses=statuses, include_unavailable=False)
        self.assertNotIn("sensors", {feature.key for feature in visible})
        self.assertEqual(len(visible), len(FEATURES) - 1)

    def test_group_counts_respect_search(self):
        counts = group_counts(query="cpu")
        self.assertEqual(counts["all"], 2)  # Processes and Benchmarks.
        self.assertEqual(counts["monitor"], 1)
        self.assertEqual(counts["tools"], 1)


class FeatureStatusTests(unittest.TestCase):
    def test_status_normalization_and_badges(self):
        status = normalize_status({"state": "attention", "detail": "Hot", "count": 3})
        self.assertEqual(status, FeatureStatus("attention", "Hot", 3))
        self.assertEqual(status_badge(status), ("3 ATTENTION", "danger"))
        self.assertEqual(status_badge("running"), ("RUNNING", "accent"))

    def test_defaults_cover_entire_catalog_and_unknown_keys_are_ignored(self):
        statuses = normalize_statuses({"alerts": "attention", "unknown": "error"})
        self.assertEqual(set(statuses), {feature.key for feature in FEATURES})
        self.assertEqual(statuses["alerts"].state, "attention")
        self.assertEqual(statuses["history"].state, "ready")

    def test_invalid_state_and_negative_count_are_rejected(self):
        with self.assertRaises(ValueError):
            FeatureStatus("mystery")
        with self.assertRaises(ValueError):
            FeatureStatus(count=-1)


class NavigationTests(unittest.TestCase):
    def test_navigation_state_is_immutable_and_safe(self):
        state = LabNavigationState()
        filtered = state.with_group("tools").with_query("json")
        self.assertEqual(state, LabNavigationState())
        self.assertEqual(tuple(item.key for item in filtered.visible()), ("reports",))
        selected = filtered.with_selection("report")
        self.assertEqual(selected.selected, "reports")
        self.assertEqual(selected.with_group("invalid").group, "all")

    def test_responsive_columns_and_rows(self):
        self.assertEqual(responsive_columns(600), 1)
        self.assertEqual(responsive_columns(860), 2)
        self.assertEqual(responsive_columns(1600), 3)
        rows = feature_rows(FEATURES[:5], 2)
        self.assertEqual(tuple(len(row) for row in rows), (2, 2, 1))

    def test_invalid_feature_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            FeatureSpec("bad key", "Bad", "Bad", "tools", "OPEN")
        with self.assertRaises(ValueError):
            FeatureSpec("bad", "Bad", "Bad", "unknown", "OPEN")


if __name__ == "__main__":
    unittest.main()
