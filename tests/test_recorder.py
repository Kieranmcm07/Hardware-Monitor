#   Made by Kieranmcm07 on GitHub
#   GitHub: https://github.com/Kieranmcm07

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hardware_monitor.recorder import SessionRecorder


def snapshot(timestamp=100.0, cpu=25.0, memory=40.0, disk=30.0, drives=None):
    if drives is None:
        drives = [SimpleNamespace(name="C:", used_percent=disk, free_gib=500.0)]
    return SimpleNamespace(
        captured_at=timestamp,
        monotonic_at=timestamp,
        cpu_usage_percent=cpu,
        memory_used_percent=memory,
        memory_used_gib=12.5,
        system_drive="C:",
        disk_used_percent=disk,
        disk_free_gib=500.0,
        drives=tuple(drives),
    )


class RecorderTests(unittest.TestCase):
    def test_capture_pause_resume_and_summary(self):
        recorder = SessionRecorder()
        recorder.capture(snapshot(100, 20, 40))
        recorder.pause(101)
        self.assertIsNone(recorder.capture(snapshot(101, 99, 99)))
        recorder.resume(102)
        recorder.capture(snapshot(102, 60, 50))
        recorder.capture(snapshot(103, 40, 60))
        summary = recorder.summary()
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["cpu_average"], 40.0)
        self.assertEqual(summary["cpu_peak"], 60.0)
        self.assertEqual(summary["memory_average"], 50.0)
        self.assertEqual(summary["duration_seconds"], 2.0)

    def test_alerts_are_explicit_thresholds(self):
        recorder = SessionRecorder()
        sample = recorder.capture(snapshot(cpu=90, memory=86, disk=91))
        self.assertIn("CPU >= 85%", sample.alert)
        self.assertIn("RAM >= 85%", sample.alert)
        self.assertIn("storage >= 90%", sample.alert)

    def test_other_fixed_drive_can_trigger_alert(self):
        drives = (
            SimpleNamespace(name="C:", used_percent=20.0, free_gib=800.0),
            SimpleNamespace(name="D:", used_percent=95.0, free_gib=50.0),
        )
        recorder = SessionRecorder()
        sample = recorder.capture(snapshot(disk=20.0, drives=drives))
        self.assertIn("D: storage >= 90%", sample.alert)
        self.assertNotIn("C: storage >= 90%", sample.alert)
        self.assertIn("D: 95.0% used", sample.fixed_drives)

    def test_csv_export(self):
        recorder = SessionRecorder()
        network = SimpleNamespace(
            download_bps=2048.5,
            upload_bps=512.25,
            session_received_bytes=4096,
            session_sent_bytes=1024,
        )
        recorder.capture(snapshot(), network)
        with tempfile.TemporaryDirectory() as directory:
            path = recorder.export_csv(Path(directory) / "session.csv")
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["system_drive"], "C:")
        self.assertIn("C: 30.0% used", rows[0]["fixed_drives"])
        self.assertEqual(rows[0]["download_bps"], "2048.5")
        self.assertEqual(rows[0]["upload_bps"], "512.25")
        self.assertEqual(rows[0]["network_received_session_bytes"], "4096")
        self.assertEqual(rows[0]["network_sent_session_bytes"], "1024")

    def test_capture_without_network_is_backwards_compatible(self):
        recorder = SessionRecorder()
        sample = recorder.capture(snapshot())
        self.assertIsNone(sample.download_bps)
        self.assertIsNone(sample.upload_bps)
        self.assertIsNone(sample.network_received_session_bytes)
        self.assertIsNone(sample.network_sent_session_bytes)

    def test_retention_is_bounded_while_summary_covers_full_session(self):
        recorder = SessionRecorder(max_samples=2)
        recorder.capture(snapshot(100, 10, 20))
        recorder.capture(snapshot(101, 20, 30))
        recorder.capture(snapshot(102, 30, 40))

        self.assertEqual(recorder.sample_count, 2)
        self.assertEqual([sample.timestamp for sample in recorder.samples], [101.0, 102.0])
        summary = recorder.summary()
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["retained_samples"], 2)
        self.assertEqual(summary["duration_seconds"], 2.0)
        self.assertEqual(summary["cpu_average"], 20.0)

    def test_reset_clears_incremental_statistics_and_timing(self):
        recorder = SessionRecorder()
        recorder.capture(snapshot(100, 90, 90))
        recorder.capture(snapshot(105, 80, 80))
        recorder.reset()

        self.assertEqual(recorder.sample_count, 0)
        self.assertEqual(
            recorder.summary(),
            {
                "samples": 0,
                "retained_samples": 0,
                "duration_seconds": 0.0,
                "cpu_average": None,
                "cpu_peak": None,
                "memory_average": None,
                "memory_peak": None,
                "alert_samples": 0,
            },
        )

    def test_snapshot_started_before_resume_does_not_move_timer_backwards(self):
        recorder = SessionRecorder()
        recorder.capture(snapshot(100))
        recorder.pause(101)
        recorder.resume(102)
        self.assertIsNone(recorder.capture(snapshot(101.5)))
        recorder.capture(snapshot(103))
        self.assertEqual(recorder.summary()["duration_seconds"], 2.0)

    def test_invalid_retention_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            SessionRecorder(max_samples=0)


if __name__ == "__main__":
    unittest.main()
