"""Standard-library regression tests for the NEXUS v4 core modules.

Run from the repository root with::

    python -m unittest -v v4_test_core

The suite deliberately avoids third-party fixtures, native process enumeration, and
unbounded background work.  All temporary files are created below the current
working directory.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import threading
import time
import unittest
import uuid
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:  # Installed package test suite.
    from hardware_monitor.alerts import (
        DEFAULT_RULES, AlertEngine, AlertEvent, AlertRule, metrics_from_snapshot,
    )
    from hardware_monitor.benchmarks import (
        BenchmarkResult, BenchmarkRunner, cpu_benchmark, disk_benchmark,
        memory_benchmark,
    )
    from hardware_monitor.history import HistorySample, HistoryStore, summarize
    from hardware_monitor.processes import (
        ProcessInfo, ProcessSnapshot, ProcessTracker, RawProcess,
        _parse_linux_stat, take_process_snapshot,
    )
    from hardware_monitor.report import (
        AUTHOR, build_report, report_html, report_json, write_report,
    )
    from hardware_monitor.settings import (
        ACCENT_PRESETS, METRICS, SETTINGS_VERSION, AppSettings, SettingsStore,
        config_directory, data_directory,
    )
except ImportError:  # Standalone staging modules.
    from v4_alerts import (
        DEFAULT_RULES, AlertEngine, AlertEvent, AlertRule, metrics_from_snapshot,
    )
    from v4_benchmarks import (
        BenchmarkResult, BenchmarkRunner, cpu_benchmark, disk_benchmark,
        memory_benchmark,
    )
    from v4_history import HistorySample, HistoryStore, summarize
    from v4_processes import (
        ProcessInfo, ProcessSnapshot, ProcessTracker, RawProcess,
        _parse_linux_stat, take_process_snapshot,
    )
    from v4_report import AUTHOR, build_report, report_html, report_json, write_report
    from v4_settings import (
        ACCENT_PRESETS, METRICS, SETTINGS_VERSION, AppSettings, SettingsStore,
        config_directory, data_directory,
    )


class WorkspaceTemporaryDirectoryTests(unittest.TestCase):
    """Give file-backed tests an always-cleaned directory inside the repo."""

    def setUp(self) -> None:
        super().setUp()
        root = Path.cwd() / "v4_test_tmp"
        root.mkdir(exist_ok=True)
        self.temp_dir = root / uuid.uuid4().hex
        self.temp_dir.mkdir()

        def cleanup() -> None:
            # sqlite3 connections participate in GC; collect before removing a
            # Windows database file so leaked query handles cannot stall cleanup.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                gc.collect()
            shutil.rmtree(self.temp_dir)
            try:
                root.rmdir()
            except OSError:
                # Another test process may still own a sibling directory.
                pass

        self.addCleanup(cleanup)


class SettingsTests(WorkspaceTemporaryDirectoryTests):
    def test_public_constants_and_directory_helpers(self) -> None:
        self.assertEqual(SETTINGS_VERSION, 1)
        self.assertIn("red", ACCENT_PRESETS)
        self.assertIn("cpu", METRICS)

        config_base = self.temp_dir / "config-base"
        data_base = self.temp_dir / "data-base"
        environment = {
            "APPDATA": str(config_base),
            "LOCALAPPDATA": str(data_base),
            "XDG_CONFIG_HOME": str(config_base),
            "XDG_DATA_HOME": str(data_base),
        }
        with patch.dict(os.environ, environment, clear=False):
            config = config_directory("Test Monitor")
            data = data_directory("Test Monitor")

        if os.name == "nt":
            self.assertEqual(config, config_base / "Test Monitor")
            self.assertEqual(data, data_base / "Test Monitor")
        else:
            self.assertEqual(config, config_base / "test-monitor")
            self.assertEqual(data, data_base / "test-monitor")
        self.assertFalse(config.exists())
        self.assertFalse(data.exists())

    def test_settings_clamp_validate_and_serialize(self) -> None:
        value = AppSettings.from_mapping(
            {
                "refresh_seconds": -4,
                "history_days": 9999,
                "alerts_enabled": "false",
                "accent": "green",
                "dashboard_metrics": ["cpu", "cpu", "bad", "memory"],
                "overlay_opacity": 10,
                "diagnostics_host": "bad host",
            }
        )
        self.assertEqual(value.refresh_seconds, 0.25)
        self.assertEqual(value.history_days, 365)
        self.assertIs(value.alerts_enabled, False)
        self.assertEqual(value.accent, "red")
        self.assertEqual(value.dashboard_metrics, ("cpu", "memory"))
        self.assertEqual(value.overlay_opacity, 1)
        self.assertEqual(value.diagnostics_host, "1.1.1.1")
        serialized = value.as_dict()
        self.assertEqual(serialized["dashboard_metrics"], ["cpu", "memory"])
        self.assertEqual(serialized["version"], SETTINGS_VERSION)

    def test_settings_atomic_round_trip(self) -> None:
        store = SettingsStore(self.temp_dir / "nested" / "settings.json")
        expected = AppSettings(accent="ruby", dashboard_metrics=("network", "cpu"))
        store.save(expected)
        self.assertEqual(store.load(), expected)
        saved = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], SETTINGS_VERSION)
        self.assertEqual(list(store.path.parent.glob("*.tmp")), [])

    def test_corrupt_and_non_object_settings_use_defaults(self) -> None:
        path = self.temp_dir / "settings.json"
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(SettingsStore(path).load(), AppSettings())
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(SettingsStore(path).load(), AppSettings())


class AlertTests(unittest.TestCase):
    def test_default_rules_are_unique_and_cover_core_metrics(self) -> None:
        self.assertEqual(
            tuple(rule.key for rule in DEFAULT_RULES),
            ("cpu", "memory", "storage", "temperature"),
        )

    def test_alert_requires_hold_and_resolves_with_hysteresis(self) -> None:
        engine = AlertEngine(
            [AlertRule("cpu", "CPU", 80, hold_seconds=2, hysteresis=5)]
        )
        self.assertEqual(engine.evaluate({"cpu": 90}, 10), ())
        self.assertEqual(engine.evaluate({"cpu": 90}, 11.9), ())
        raised = engine.evaluate({"cpu": 90}, 12)
        self.assertEqual(
            raised,
            (AlertEvent("cpu", "CPU", "raised", 90.0, 80, "%", 12.0,
                        "CPU reached 90.0%"),),
        )
        self.assertEqual(engine.active_keys, ("cpu",))
        self.assertEqual(engine.evaluate({"cpu": 78}, 13), ())
        resolved = engine.evaluate({"cpu": 75}, 14)
        self.assertEqual(resolved[0].kind, "resolved")
        self.assertEqual(engine.active_keys, ())

    def test_alert_cooldown_prevents_realert(self) -> None:
        rule = AlertRule("cpu", "CPU", 80, hold_seconds=0, cooldown_seconds=10)
        engine = AlertEngine([rule])
        self.assertEqual(engine.evaluate({"cpu": 90}, 0)[0].kind, "raised")
        self.assertEqual(engine.evaluate({"cpu": 0}, 1)[0].kind, "resolved")
        self.assertEqual(engine.evaluate({"cpu": 90}, 2), ())
        self.assertEqual(engine.evaluate({"cpu": 90}, 10)[0].kind, "raised")

    def test_resolve_all_and_snapshot_adapter(self) -> None:
        engine = AlertEngine([AlertRule("cpu", "CPU", 80, hold_seconds=0)])
        engine.evaluate({"cpu": 99}, 1)
        resolved = engine.resolve_all(2)
        self.assertIsNone(resolved[0].value)
        self.assertEqual(engine.resolve_all(3), ())

        snapshot = SimpleNamespace(
            cpu_usage_percent=12,
            memory_used_percent=34,
            disk_used_percent=56,
        )
        self.assertEqual(
            metrics_from_snapshot(snapshot, 67),
            {"cpu": 12, "memory": 34, "storage": 56, "temperature": 67},
        )

    def test_invalid_rules_are_rejected_and_invalid_metrics_are_ignored(self) -> None:
        invalid_rules = (
            AlertRule,
            lambda: AlertRule("cpu", "CPU", 80, hold_seconds=-1),
            lambda: AlertRule("cpu", "CPU", 80, hysteresis=-1),
            lambda: AlertRule("cpu", "CPU", 80, cooldown_seconds=-1),
        )
        with self.assertRaises(ValueError):
            invalid_rules[0]("", "Empty", 1)
        for factory in invalid_rules[1:]:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

        with self.assertRaises(ValueError):
            AlertEngine([AlertRule("x", "X", 1), AlertRule("x", "X2", 2)])
        engine = AlertEngine([AlertRule("cpu", "CPU", 80, hold_seconds=0)])
        self.assertEqual(engine.evaluate({"cpu": "not numeric"}, 1), ())
        self.assertEqual(engine.evaluate({"cpu": float("nan")}, 2), ())
        self.assertEqual(engine.evaluate({"cpu": float("inf")}, 3), ())
        self.assertEqual(engine.evaluate({"cpu": None}, 2), ())


class HistoryTests(WorkspaceTemporaryDirectoryTests):
    def _store(self, name: str = "history.db", **kwargs: object) -> HistoryStore:
        store = HistoryStore(self.temp_dir / name, **kwargs)
        self.addCleanup(store.close, 2.0)
        return store

    def test_history_background_round_trip_and_snapshot_adapter(self) -> None:
        store = self._store()
        self.assertTrue(store.add(HistorySample(100, 10, 20, 30, 40, 50, 60)))
        self.assertTrue(
            store.add_snapshot(
                SimpleNamespace(
                    captured_at=101,
                    cpu_usage_percent=11,
                    memory_used_percent=21,
                    disk_used_percent=31,
                ),
                temperature_c=41,
                network_down_bps=51,
                network_up_bps=61,
            )
        )
        self.assertTrue(store.flush(timeout=2.0))
        rows = store.query(99, 102)
        store.close(timeout=2.0)
        self.assertEqual([row.timestamp for row in rows], [100, 101])
        self.assertEqual(rows[0].temperature_c, 40)
        self.assertEqual(rows[1].network_up_bps, 61)
        self.assertIsNone(store.error)

    def test_history_prunes_old_samples(self) -> None:
        path = self.temp_dir / "history.db"
        old_store = HistoryStore(path, retention_days=365)
        self.addCleanup(old_store.close, 2.0)
        old_store.add(HistorySample(time.time() - 400 * 86_400, 1, 2, 3))
        self.assertTrue(old_store.flush(timeout=2.0))
        old_store.close(timeout=2.0)

        store = HistoryStore(path, retention_days=30)
        self.addCleanup(store.close, 2.0)
        store.close(timeout=2.0)
        self.assertEqual(store.query(0), ())

    def test_history_summary_ignores_missing_values(self) -> None:
        result = summarize(
            [
                HistorySample(1, 10, None, 30),
                HistorySample(2, 30, None, 50),
            ]
        )
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["cpu_average"], 20)
        self.assertEqual(result["cpu_peak"], 30)
        self.assertEqual(result["storage_peak"], 50)
        self.assertIsNone(result["memory_average"])
        self.assertIsNone(result["temperature_peak"])

    def test_store_clamps_retention_and_autostarts_on_add(self) -> None:
        store = self._store(autostart=False, retention_days=0)
        self.assertEqual(store.retention_days, 1)
        self.assertEqual(store.query(0), ())
        self.assertTrue(store.add(HistorySample(1, 1, 2, 3)))
        self.assertTrue(store.flush(timeout=2.0))
        self.assertEqual(store.query(0, 2), (HistorySample(1, 1, 2, 3),))


class ProcessTests(unittest.TestCase):
    def test_linux_stat_parser_handles_parentheses_in_name(self) -> None:
        tail = ["S"] + ["0"] * 19
        tail[11] = "100"
        tail[12] = "50"
        tail[17] = "4"
        tail[19] = "900"
        parsed = _parse_linux_stat("42 (a tricky) name) " + " ".join(tail))
        self.assertEqual(parsed, ("a tricky) name", 100, 50, 4, 900))

    def test_process_tracker_cpu_memory_sorting_and_pid_reuse(self) -> None:
        tracker = ProcessTracker(logical_cpus=2)
        first = [
            RawProcess(1, "slow", 1, 10 * 1024**2, 100),
            RawProcess(2, "fast", 2, 20 * 1024**2, 200),
        ]
        initial = tracker.update(first, captured_at=10, total_memory=100 * 1024**2)
        self.assertTrue(all(item.cpu_percent is None for item in initial.processes))

        second = [
            RawProcess(1, "slow", 1.2, 10 * 1024**2, 100),
            RawProcess(2, "fast", 3, 20 * 1024**2, 200),
        ]
        result = tracker.update(
            second,
            captured_at=11,
            total_memory=100 * 1024**2,
            inaccessible_count=3,
        )
        self.assertIsInstance(result, ProcessSnapshot)
        self.assertEqual([item.pid for item in result.processes], [2, 1])
        self.assertEqual(result.processes[0].cpu_percent, 50)
        self.assertEqual(result.processes[0].memory_mib, 20)
        self.assertEqual(result.processes[0].memory_percent, 20)
        self.assertEqual(result.inaccessible_count, 3)

        reused = tracker.update(
            [RawProcess(2, "new", 0.1, 1, 999)], captured_at=12
        )
        self.assertIsNone(reused.processes[0].cpu_percent)

    def test_tracker_caps_process_at_total_system_100_percent(self) -> None:
        tracker = ProcessTracker(logical_cpus=1)
        tracker.update([RawProcess(1, "x", 0, 0, 1)], captured_at=0)
        current = tracker.update(
            [RawProcess(1, "x", 10, 0, 1)], captured_at=1
        )
        self.assertEqual(current.processes[0].cpu_percent, 100)

    def test_take_process_snapshot_uses_supplied_tracker(self) -> None:
        expected = ProcessSnapshot(
            1.0,
            (ProcessInfo(7, "test", 1.0, 2.0, 3.0, "", 4),),
            5,
        )
        tracker = Mock(spec=ProcessTracker)
        tracker.sample.return_value = expected
        self.assertIs(take_process_snapshot(tracker), expected)
        tracker.sample.assert_called_once_with()


class BenchmarkTests(WorkspaceTemporaryDirectoryTests):
    @staticmethod
    def _await_runner_stopped(runner: BenchmarkRunner, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while runner.running and time.monotonic() < deadline:
            time.sleep(0.005)
        return not runner.running

    def _cancel_and_await_runner(self, runner: BenchmarkRunner) -> None:
        runner.cancel()
        self.assertTrue(
            self._await_runner_stopped(runner, 2.0),
            "benchmark worker did not stop after cancellation",
        )

    def test_short_cpu_and_memory_benchmarks_pass(self) -> None:
        cpu_progress: list[tuple[str, float]] = []
        cpu = cpu_benchmark(0.1, progress=lambda name, value: cpu_progress.append((name, value)))
        self.assertEqual(cpu.status, "passed")
        self.assertGreater(cpu.score or 0, 0)
        self.assertTrue(cpu_progress)
        self.assertTrue(all(name == "CPU" and 0 <= value <= 1 for name, value in cpu_progress))

        memory = memory_benchmark(size_mib=1, rounds=2)
        self.assertEqual(memory.status, "passed")
        self.assertGreater(memory.score or 0, 0)

    def test_benchmarks_honor_existing_cancellation(self) -> None:
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(cpu_benchmark(cancel=cancel).status, "cancelled")
        self.assertEqual(memory_benchmark(1, 1, cancel=cancel).status, "cancelled")
        results = disk_benchmark(1, self.temp_dir, cancel=cancel)
        self.assertTrue(all(result.status == "cancelled" for result in results))
        self.assertEqual(list(self.temp_dir.glob("nexus-benchmark-*")), [])

    def test_disk_benchmark_uses_and_removes_workspace_temp_file(self) -> None:
        progress: list[tuple[str, float]] = []
        write, read = disk_benchmark(
            1,
            self.temp_dir,
            progress=lambda name, value: progress.append((name, value)),
        )
        self.assertEqual((write.status, read.status), ("passed", "passed"))
        self.assertGreater(write.score or 0, 0)
        self.assertGreater(read.score or 0, 0)
        self.assertIn(("Disk read", 1.0), progress)
        self.assertEqual(list(self.temp_dir.glob("nexus-benchmark-*")), [])

    def test_runner_cancellation_is_bounded_and_prevents_later_stages(self) -> None:
        runner = BenchmarkRunner()
        self.addCleanup(self._cancel_and_await_runner, runner)
        started = threading.Event()
        completed = threading.Event()
        captured: list[tuple[BenchmarkResult, ...]] = []

        def controlled_cpu(
            duration: float = 1.5,
            cancel: threading.Event | None = None,
            progress: object | None = None,
        ) -> BenchmarkResult:
            del duration, progress
            started.set()
            if cancel is not None and cancel.wait(timeout=2.0):
                return BenchmarkResult("CPU", "cancelled", None, "MiB/s", 0)
            return BenchmarkResult("CPU", "failed", None, "MiB/s", 2.0,
                                   "test cancellation timeout")

        def complete(results: tuple[BenchmarkResult, ...]) -> None:
            captured.append(results)
            completed.set()

        benchmark_module = BenchmarkRunner.__module__
        with patch(f"{benchmark_module}.cpu_benchmark", side_effect=controlled_cpu), patch(
            f"{benchmark_module}.memory_benchmark",
            side_effect=AssertionError("memory stage ran after cancellation"),
        ) as memory, patch(
            f"{benchmark_module}.disk_benchmark",
            side_effect=AssertionError("disk stage ran after cancellation"),
        ) as disk:
            self.assertTrue(runner.start(complete))
            self.assertTrue(started.wait(timeout=1.0))
            self.assertTrue(runner.running)
            self.assertFalse(runner.start(complete))
            runner.cancel()
            self.assertTrue(completed.wait(timeout=2.0))
            self.assertTrue(self._await_runner_stopped(runner, timeout=1.0))

        self.assertEqual(captured[0][0].status, "cancelled")
        memory.assert_not_called()
        disk.assert_not_called()


def _report_snapshot() -> SimpleNamespace:
    drive = SimpleNamespace(name="C:", total_gib=100, free_gib=25, used_percent=75)
    return SimpleNamespace(
        operating_system="TestOS <script>alert(1)</script>",
        processor="CPU",
        physical_cores=4,
        logical_cpus=8,
        memory_installed_gib=16,
        cpu_usage_percent=10,
        memory_used_percent=20,
        disk_used_percent=30,
        battery_percent=None,
        uptime_seconds=123,
        drives=(drive,),
        computer="PRIVATE-PC",
        network_interfaces=("10.0.0.1",),
    )


class ReportTests(WorkspaceTemporaryDirectoryTests):
    def test_report_serializes_enums_and_anonymizes_mount_paths(self) -> None:
        snapshot = _report_snapshot()
        snapshot.drives = (
            SimpleNamespace(
                name="/home/private-user/External Drive",
                total_gib=100,
                free_gib=75,
                used_percent=25,
            ),
        )
        report = build_report(
            snapshot,
            sensors=[
                SimpleNamespace(
                    label="GPU", kind=SimpleNamespace(value="temperature"),
                    value=45, unit="°C",
                )
            ],
            drive_health=[
                SimpleNamespace(
                    model="SSD", health=SimpleNamespace(value="passed"),
                    temperature_c=35,
                )
            ],
        )
        self.assertEqual(report["drives"][0]["name"], "Volume 1")
        self.assertEqual(report["sensors"][0]["kind"], "temperature")
        self.assertEqual(report["drive_health"][0]["status"], "passed")
        self.assertNotIn("private-user", report_json(report))

    def test_report_allowlist_excludes_private_identifiers_and_escapes_html(self) -> None:
        report = build_report(
            _report_snapshot(),
            hardware=SimpleNamespace(
                motherboard="Board", bios_version="1.2", gpu_names=("GPU",)
            ),
            sensors=[{"label": "CPU <temp>", "kind": "temperature", "value": 50, "unit": "C"}],
            drive_health=[
                {
                    "model": "Drive <b>",
                    "serial": "SECRET",
                    "device": "/dev/secret",
                    "status": "ok",
                }
            ],
            benchmarks=[BenchmarkResult("CPU", "passed", 12.5, "MiB/s", 0.1)],
        )
        raw = report_json(report)
        self.assertNotIn("PRIVATE-PC", raw)
        self.assertNotIn("10.0.0.1", raw)
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("/dev/secret", raw)
        self.assertEqual(report["report"]["author"], AUTHOR)
        self.assertEqual(report["system"]["gpu_names"], ["GPU"])
        self.assertEqual(report["benchmarks"][0]["score"], 12.5)

        rendered = report_html(report)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("Drive &lt;b&gt;", rendered)
        self.assertIn("CPU &lt;temp&gt;", rendered)

    def test_report_json_is_valid_and_newline_terminated(self) -> None:
        report = build_report(_report_snapshot())
        rendered = report_json(report)
        self.assertTrue(rendered.endswith("\n"))
        self.assertEqual(json.loads(rendered), report)

    def test_write_report_round_trips_json_and_html_atomically(self) -> None:
        report = build_report(_report_snapshot())
        output = write_report(self.temp_dir / "nested" / "report.json", report)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["report"]["author"],
            AUTHOR,
        )
        html_output = write_report(self.temp_dir / "report.any", report, format="html")
        self.assertTrue(html_output.read_text(encoding="utf-8").startswith("<!doctype html>"))
        self.assertEqual(list(self.temp_dir.rglob("*.tmp")), [])

    def test_write_report_rejects_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            write_report(self.temp_dir / "report.exe", {})


if __name__ == "__main__":
    unittest.main()
