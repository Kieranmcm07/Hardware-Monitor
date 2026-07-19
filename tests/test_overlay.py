import math
import types
import unittest

try:
    from hardware_monitor.overlay import (
        OverlayTelemetry,
        ScreenBounds,
        clamp_opacity,
        fit_geometry,
        format_percent,
        format_rate,
        format_temperature,
        geometry_string,
        metric_display,
        normalize_overlay_metrics,
        parse_geometry,
        top_right_geometry,
    )
except ModuleNotFoundError:
    from v4_overlay import (
        OverlayTelemetry,
        ScreenBounds,
        clamp_opacity,
        fit_geometry,
        format_percent,
        format_rate,
        format_temperature,
        geometry_string,
        metric_display,
        normalize_overlay_metrics,
        parse_geometry,
        top_right_geometry,
    )


class GeometryTests(unittest.TestCase):
    def test_negative_virtual_coordinate_round_trip(self):
        encoded = geometry_string(640, 154, -1720, 24)
        self.assertEqual(encoded, "640x154-1720+24")
        self.assertEqual(parse_geometry(encoded).as_tk(), encoded)

    def test_top_right_on_monitor_left_of_primary(self):
        bounds = ScreenBounds(-1920, 0, 0, 1080)
        geometry = top_right_geometry(600, 150, bounds, margin=24)
        self.assertEqual((geometry.x, geometry.y), (-624, 24))

    def test_fit_clamps_and_shrinks_only_when_needed(self):
        bounds = ScreenBounds(10, 20, 810, 620)
        geometry = fit_geometry(1200, 900, -500, 9999, bounds, margin=10)
        self.assertEqual(
            (geometry.width, geometry.height, geometry.x, geometry.y),
            (780, 580, 20, 30),
        )
        with self.assertRaises(ValueError):
            ScreenBounds(0, 0, 0, 100)
        with self.assertRaises(ValueError):
            parse_geometry("640 by 154")


class TelemetryHelperTests(unittest.TestCase):
    def test_formatting_handles_missing_clamped_and_non_finite_values(self):
        self.assertEqual(format_percent(None), "N/A")
        self.assertEqual(format_percent(34.56), "34.6%")
        self.assertEqual(format_percent(150), "100%")
        self.assertEqual(format_temperature(67.8), "68°C")
        self.assertEqual(format_temperature(float("inf")), "N/A")
        self.assertEqual(format_rate(1024), "1.0 KiB/s")
        self.assertEqual(clamp_opacity(0.1), 0.35)
        self.assertEqual(clamp_opacity(math.nan), 0.9)

    def test_metrics_are_ordered_deduplicated_and_have_safe_default(self):
        self.assertEqual(
            normalize_overlay_metrics(["net", "cpu", "CPU", "temp", "bad"]),
            ("network", "cpu", "temperature"),
        )
        self.assertEqual(
            normalize_overlay_metrics([]),
            ("cpu", "memory", "storage", "network"),
        )

    def test_cached_sources_are_copied_without_collecting(self):
        snapshot = types.SimpleNamespace(
            cpu_usage_percent=31.25,
            memory_used_percent=48,
            disk_used_percent=67.5,
            cpu_temperature_c=61,
            gpu_temperature_c=72,
            captured_at=1_700_000_000,
        )
        network = {"download_bps": 2048, "upload_bps": 512}
        result = OverlayTelemetry.from_cached(snapshot, network)
        self.assertEqual(result.cpu_percent, 31.25)
        self.assertEqual(result.memory_percent, 48)
        self.assertEqual(result.storage_percent, 67.5)
        self.assertEqual(result.temperature_c, 72)
        self.assertEqual(result.download_bps, 2048)
        self.assertEqual(result.upload_bps, 512)

    def test_metric_display_is_explicit(self):
        telemetry = OverlayTelemetry(
            download_bps=1024, upload_bps=512, temperature_c=62,
            battery_percent=73,
        )
        network = metric_display("network", telemetry)
        self.assertEqual(network.label, "NETWORK DOWN")
        self.assertEqual(network.value, "1.0 KiB/s")
        self.assertEqual(network.detail, "UP 512 B/s")
        self.assertEqual(metric_display("temperature", telemetry).value, "62°C")
        self.assertEqual(metric_display("battery", telemetry).value, "73%")
        with self.assertRaises(ValueError):
            metric_display("unknown", telemetry)


if __name__ == "__main__":
    unittest.main()
