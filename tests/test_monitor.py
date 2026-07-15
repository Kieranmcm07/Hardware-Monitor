import tempfile
import unittest
from pathlib import Path

from hardware_monitor.monitor import cpu_self_test, disk_self_test, take_snapshot


class MonitorTests(unittest.TestCase):
    def test_snapshot_has_sensible_values(self):
        snapshot = take_snapshot(Path.home().anchor)
        self.assertGreaterEqual(snapshot.logical_cpus, 1)
        self.assertGreater(snapshot.disk_total_gb, 0)
        self.assertGreaterEqual(snapshot.disk_used_percent, 0)
        self.assertLessEqual(snapshot.disk_used_percent, 100)

    def test_cpu_self_test(self):
        result = cpu_self_test(0.1)
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["sha256_blocks"], 0)

    def test_disk_self_test(self):
        result = disk_self_test(1)
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()

