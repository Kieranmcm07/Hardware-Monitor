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
        recorder.pause()
        self.assertIsNone(recorder.capture(snapshot(101, 99, 99)))
        recorder.resume()
        recorder.capture(snapshot(102, 60, 50))
        summary = recorder.summary()
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["cpu_average"], 40.0)
        self.assertEqual(summary["cpu_peak"], 60.0)

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
        recorder.capture(snapshot())
        with tempfile.TemporaryDirectory() as directory:
            path = recorder.export_csv(Path(directory) / "session.csv")
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["system_drive"], "C:")
        self.assertIn("C: 30.0% used", rows[0]["fixed_drives"])


if __name__ == "__main__":
    unittest.main()
