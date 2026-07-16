import ctypes
import tempfile
import unittest
from pathlib import Path

from hardware_monitor.main import power_status
from hardware_monitor import monitor
from hardware_monitor.monitor import cpu_self_test, disk_self_test, take_snapshot


class MonitorTests(unittest.TestCase):
    def test_native_network_table_parser_filters_rows_and_keeps_64_bit_counters(self):
        def row(luid, *, flags=0x01, status=1, connected=1, kind=6):
            native = monitor._MibIfRow2()
            native.interface_luid = luid
            native.interface_index = luid
            native.alias = f"Adapter {luid}"
            native.description = "Synthetic Windows adapter"
            native.type = kind
            native.interface_flags = flags
            native.operational_status = status
            native.media_connect_state = connected
            native.receive_link_speed = 1_000_000_000
            native.transmit_link_speed = 1_000_000_000
            native.in_octets = 5 * 1024**3 + 123
            native.out_octets = 7 * 1024**3 + 456
            return native

        rows = (
            row(1),
            row(2, flags=0x03),       # Windows filter interface
            row(3, flags=0x81),       # endpoint interface
            row(4, status=2),         # not operational
            row(5, kind=24),          # software loopback
        )
        offset = monitor._MibIfTable2.table.offset
        row_size = ctypes.sizeof(monitor._MibIfRow2)
        table = ctypes.create_string_buffer(offset + row_size * len(rows))
        ctypes.c_ulong.from_buffer(table).value = len(rows)
        address = ctypes.addressof(table)
        for position, native in enumerate(rows):
            ctypes.memmove(
                address + offset + position * row_size,
                ctypes.byref(native),
                row_size,
            )

        parsed = monitor._network_interfaces_from_table(address)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].luid, 1)
        self.assertEqual(parsed[0].received_bytes, 5 * 1024**3 + 123)
        self.assertEqual(parsed[0].sent_bytes, 7 * 1024**3 + 456)

    def test_unknown_power_source_is_not_reported_as_battery(self):
        self.assertEqual(power_status(72, None), "72% (Power source unknown)")
        self.assertEqual(power_status(72, True), "72% (AC connected)")
        self.assertEqual(power_status(72, False), "72% (On battery)")
        self.assertEqual(power_status(None, None), "No battery detected")

    def test_snapshot_has_sensible_values(self):
        snapshot = take_snapshot(Path.home().anchor)
        self.assertGreaterEqual(snapshot.logical_cpus, 1)
        self.assertGreater(snapshot.disk_total_gb, 0)
        self.assertGreater(snapshot.captured_at, 0)
        self.assertGreater(snapshot.monotonic_at, 0)
        self.assertGreaterEqual(snapshot.disk_used_percent, 0)
        self.assertLessEqual(snapshot.disk_used_percent, 100)
        self.assertGreaterEqual(len(snapshot.drives), 1)
        drive_names = [drive.name for drive in snapshot.drives]
        self.assertEqual(len(drive_names), len(set(drive_names)))
        self.assertIn(snapshot.system_drive, drive_names)
        for drive in snapshot.drives:
            self.assertGreater(drive.total_gib, 0)
            self.assertGreaterEqual(drive.free_gib, 0)
            self.assertLessEqual(drive.free_gib, drive.total_gib)
            self.assertGreaterEqual(drive.used_percent, 0)
            self.assertLessEqual(drive.used_percent, 100)
        adapter_ids = [adapter.luid for adapter in snapshot.network_interfaces]
        self.assertEqual(len(adapter_ids), len(set(adapter_ids)))
        for adapter in snapshot.network_interfaces:
            self.assertGreater(adapter.luid, 0)
            self.assertGreater(adapter.index, 0)
            self.assertTrue(adapter.alias)
            self.assertTrue(adapter.description)
            self.assertIn(
                adapter.kind,
                {"Ethernet", "Wi-Fi", "Mobile broadband", "Network"},
            )
            self.assertGreaterEqual(adapter.receive_link_bps, 0)
            self.assertGreaterEqual(adapter.transmit_link_bps, 0)
            self.assertGreaterEqual(adapter.received_bytes, 0)
            self.assertGreaterEqual(adapter.sent_bytes, 0)

        data = snapshot.as_dict()
        self.assertIn("monotonic_at", data)
        self.assertIn("network_interfaces", data)

    def test_cpu_self_test(self):
        result = cpu_self_test(0.1)
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["sha256_blocks"], 0)

    def test_disk_self_test(self):
        result = disk_self_test(1)
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
