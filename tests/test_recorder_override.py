import unittest
from types import SimpleNamespace

from hardware_monitor.recorder import SessionRecorder


def snapshot(monotonic_at: float = 1.0):
    return SimpleNamespace(
        captured_at=100.0 + monotonic_at,
        monotonic_at=monotonic_at,
        cpu_usage_percent=99.0,
        memory_used_percent=99.0,
        memory_used_gib=4.0,
        system_drive="C:",
        disk_used_percent=99.0,
        disk_free_gib=1.0,
        drives=(),
    )


class RecorderAlertOverrideTests(unittest.TestCase):
    def test_explicit_override_disables_legacy_thresholds(self):
        recorder = SessionRecorder()
        sample = recorder.capture(snapshot(), alert_override="")
        self.assertEqual(sample.alert, "")
        self.assertEqual(recorder.summary()["alert_samples"], 0)

    def test_explicit_override_is_recorded(self):
        recorder = SessionRecorder()
        sample = recorder.capture(snapshot(), alert_override="Custom alert")
        self.assertEqual(sample.alert, "Custom alert")
        self.assertEqual(recorder.summary()["alert_samples"], 1)


if __name__ == "__main__":
    unittest.main()
