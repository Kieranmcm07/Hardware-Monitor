from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

try:
    from hardware_monitor.diagnostics import DiagnosticResult, DiagnosticState, ProbeMethod
    from hardware_monitor.feature_windows import (
        FEATURE_WINDOW_CATALOG,
        FeatureWindowSpec,
        alert_metrics_from_snapshot,
        alert_rules_from_settings,
        diagnostic_summary_lines,
        feature_window_spec,
        format_data_rate,
        format_metric,
        history_series,
        normalize_window_key,
        process_rows,
        report_default_filename,
        scale_history_points,
        settings_with_updates,
    )
    from hardware_monitor.history import HistorySample
    from hardware_monitor.processes import ProcessInfo, ProcessSnapshot
    from hardware_monitor.settings import AppSettings
except ImportError:
    from v4_diagnostics import DiagnosticResult, DiagnosticState, ProbeMethod
    from v4_feature_windows import (
        FEATURE_WINDOW_CATALOG,
        FeatureWindowSpec,
        alert_metrics_from_snapshot,
        alert_rules_from_settings,
        diagnostic_summary_lines,
        feature_window_spec,
        format_data_rate,
        format_metric,
        history_series,
        normalize_window_key,
        process_rows,
        report_default_filename,
        scale_history_points,
        settings_with_updates,
    )
    from v4_history import HistorySample
    from v4_processes import ProcessInfo, ProcessSnapshot
    from v4_settings import AppSettings


class FeatureWindowCatalogTests(unittest.TestCase):
    def test_catalog_has_exactly_the_eight_lab_features(self) -> None:
        self.assertEqual(
            tuple(spec.key for spec in FEATURE_WINDOW_CATALOG),
            (
                "processes", "alerts", "sensors", "history",
                "diagnostics", "benchmarks", "reports", "customization",
            ),
        )
        self.assertEqual(len({spec.number for spec in FEATURE_WINDOW_CATALOG}), 8)

    def test_aliases_resolve_to_catalog_entries(self) -> None:
        self.assertEqual(normalize_window_key("Process Explorer"), "processes")
        self.assertEqual(feature_window_spec("drive-health").key, "sensors")
        self.assertEqual(feature_window_spec("settings").key, "customization")
        with self.assertRaises(KeyError):
            feature_window_spec("not-a-feature")

    def test_invalid_catalog_specs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FeatureWindowSpec("bad key", "09", "BAD", "Bad", "800x600")
        with self.assertRaises(ValueError):
            FeatureWindowSpec("valid", "XX", "BAD", "Bad", "800x600")


class FeatureWindowPureHelperTests(unittest.TestCase):
    def test_metric_and_rate_formatting_reject_non_finite_values(self) -> None:
        self.assertEqual(format_metric(12.345, "%", 1), "12.3%")
        self.assertEqual(format_metric(math.nan, "%"), "N/A")
        self.assertEqual(format_metric(True), "N/A")
        self.assertEqual(format_data_rate(1536), "1.5 KiB/s")
        self.assertEqual(format_data_rate(None), "N/A")

    def test_process_rows_filter_sort_and_limit_without_mutating_snapshot(self) -> None:
        processes = (
            ProcessInfo(2, "Browser", 12.0, 450.0, 4.5, "/apps/browser", 8),
            ProcessInfo(1, "Worker", 88.0, 120.0, 1.2, "/srv/worker", 3),
            ProcessInfo(3, "Idle", None, None, None, "", None),
        )
        snapshot = ProcessSnapshot(10.0, processes, 0)
        self.assertEqual(
            tuple(item.pid for item in process_rows(snapshot, sort_by="cpu")),
            (1, 2, 3),
        )
        self.assertEqual(
            tuple(item.pid for item in process_rows(snapshot, query="browser")),
            (2,),
        )
        self.assertEqual(
            tuple(item.pid for item in process_rows(
                snapshot, sort_by="name", descending=False, limit=2
            )),
            (2, 3),
        )
        self.assertEqual(snapshot.processes, processes)

    def test_history_helpers_sort_skip_missing_and_scale_to_canvas(self) -> None:
        samples = (
            HistorySample(30, 50, 20, 10),
            HistorySample(10, 0, 10, 10),
            HistorySample(20, None, 15, 10),
        )
        self.assertEqual(history_series(samples, "cpu"), ((10.0, 0.0), (30.0, 50.0)))
        points = scale_history_points(samples, "cpu", 200, 100, padding=10)
        self.assertEqual(points[0], (10.0, 90.0))
        self.assertEqual(points[-1], (190.0, 50.0))
        with self.assertRaises(ValueError):
            history_series(samples, "unknown")

    def test_settings_helpers_use_service_validation(self) -> None:
        settings = settings_with_updates(
            AppSettings(),
            {"cpu_alert_percent": 500, "temperature_alert_c": 20, "accent": "ruby"},
        )
        self.assertEqual(settings.cpu_alert_percent, 100)
        self.assertEqual(settings.temperature_alert_c, 40)
        self.assertEqual(settings.accent, "ruby")
        rules = {rule.key: rule for rule in alert_rules_from_settings(settings)}
        self.assertEqual(rules["cpu"].threshold, 100)
        self.assertEqual(rules["temperature"].unit, "°C")

    def test_alert_metrics_use_fullest_detected_volume(self) -> None:
        snapshot = SimpleNamespace(
            cpu_usage_percent=12,
            memory_used_percent=34,
            disk_used_percent=20,
            drives=(
                SimpleNamespace(used_percent=55),
                SimpleNamespace(used_percent=96),
            ),
        )
        metrics = alert_metrics_from_snapshot(snapshot, 71)
        self.assertEqual(metrics["storage"], 96)
        self.assertEqual(metrics["temperature"], 71)

    def test_report_filename_and_diagnostic_summary_are_deterministic(self) -> None:
        self.assertEqual(
            report_default_filename("json", timestamp=0),
            "nexus-hardware-report-19700101-000000.json",
        )
        result = DiagnosticResult(
            state=DiagnosticState.COMPLETE,
            target="example.test",
            dns_ms=1.25,
            method=ProbeMethod.ICMP,
            sent=4,
            received=3,
            failures=1,
            packet_loss_percent=25.0,
            average_ms=8.5,
            jitter_ms=0.75,
        )
        lines = diagnostic_summary_lines(result)
        self.assertIn("STATE  COMPLETE", lines)
        self.assertIn("PACKET LOSS  25.0%", lines)
        self.assertIn("AVERAGE  8.50 ms", lines)


if __name__ == "__main__":
    unittest.main()
