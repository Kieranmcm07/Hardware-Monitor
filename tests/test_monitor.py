import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hardware_monitor.main import power_status
from hardware_monitor import monitor
from hardware_monitor.monitor import cpu_self_test, disk_self_test, take_snapshot


class MonitorTests(unittest.TestCase):
    @staticmethod
    def write_sensor(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows IP Helper ABI test")
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

    def test_linux_cpu_memory_uptime_and_distribution_parsers(self):
        cpuinfo = """
processor : 0
physical id : 0
core id : 0
cpu cores : 2
model name : Example Linux CPU
cpu MHz : 3600.0

processor : 1
physical id : 0
core id : 0
cpu cores : 2
model name : Example Linux CPU
cpu MHz : 4100.0

processor : 2
physical id : 0
core id : 1
cpu cores : 2
model name : Example Linux CPU
cpu MHz : 4200.0

processor : 3
physical id : 0
core id : 1
cpu cores : 2
model name : Example Linux CPU
cpu MHz : 3900.0
"""
        name, physical, logical, mhz = monitor._parse_linux_cpuinfo(cpuinfo)
        self.assertEqual(name, "Example Linux CPU")
        self.assertEqual(physical, 2)
        self.assertEqual(logical, 4)
        self.assertEqual(mhz, 4200)

        self.assertEqual(
            monitor._parse_linux_cpu_times("cpu  100 20 30 400 50 10 5 2 1 1\n"),
            (450, 617),
        )
        memory = monitor._parse_linux_meminfo(
            "MemTotal: 16777216 kB\nMemAvailable: 4194304 kB\n"
        )
        self.assertEqual(memory["MemTotal"], 16 * 1024**3)
        self.assertEqual(memory["MemAvailable"], 4 * 1024**3)
        self.assertEqual(monitor._parse_linux_uptime("12345.67 9000.00"), 12345.67)
        self.assertEqual(
            monitor._parse_os_release('NAME="Example"\nPRETTY_NAME="Example Linux 42"\n'),
            "Example Linux 42",
        )
        with (
            patch.object(monitor, "_is_linux", return_value=True),
            patch.object(
                monitor, "_linux_cpu_times", side_effect=((100, 1000), (110, 1100))
            ),
            patch.object(monitor.time, "sleep"),
        ):
            self.assertEqual(monitor.cpu_usage(), 90.0)

    def test_linux_hardware_collectors_use_proc_and_sys_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            proc_root = root / "procfs"
            sys_root = root / "sysfs"
            self.write_sensor(
                proc_root / "cpuinfo",
                "processor: 0\nmodel name: Test CPU\nphysical id: 0\n"
                "core id: 0\ncpu cores: 1\ncpu MHz: 3200\n",
            )
            self.write_sensor(
                proc_root / "meminfo",
                "MemTotal: 8388608 kB\nMemAvailable: 2097152 kB\n",
            )
            self.write_sensor(proc_root / "uptime", "4567.25 1200.00\n")
            self.write_sensor(
                sys_root / "devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq",
                "5100000\n",
            )
            self.write_sensor(sys_root / "class/dmi/id/board_vendor", "Example Corp\n")
            self.write_sensor(sys_root / "class/dmi/id/board_name", "Example Board\n")
            self.write_sensor(sys_root / "class/dmi/id/bios_version", "1.2.3\n")
            gpu = sys_root / "class/drm/card0/device"
            self.write_sensor(gpu / "vendor", "0x10de\n")
            self.write_sensor(gpu / "device", "0x2684\n")
            self.write_sensor(gpu / "uevent", "DRIVER=nvidia\nPCI_ID=10DE:2684\n")

            info = monitor._linux_hardware_info(proc_root, sys_root)
            self.assertEqual(info.cpu_name, "Test CPU")
            self.assertEqual(info.physical_cores, 1)
            self.assertEqual(info.logical_cpus, 1)
            self.assertEqual(info.cpu_max_mhz, 5100)
            self.assertEqual(info.motherboard, "Example Corp Example Board")
            self.assertEqual(info.bios_version, "1.2.3")
            self.assertEqual(info.installed_memory_gib, 8.0)
            self.assertTrue(any("NVIDIA" in gpu_name for gpu_name in info.gpu_names))
            self.assertEqual(
                monitor._linux_memory_info(proc_root),
                (8.0, 6.0, 2.0, 75.0),
            )
            self.assertEqual(monitor._linux_uptime_seconds(proc_root), 4567.25)

    def test_linux_physical_cores_are_not_guessed_from_thread_count(self):
        cpuinfo = "processor: 0\nmodel name: Virtual CPU\n\nprocessor: 1\n"
        _, physical, logical, _ = monitor._parse_linux_cpuinfo(cpuinfo)
        self.assertIsNone(physical)
        self.assertEqual(logical, 2)

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            sys_root = Path(folder) / "sysfs"
            for cpu, package, core in ((0, 0, 0), (1, 0, 0), (2, 0, 1), (3, 0, 1)):
                topology = sys_root / "devices/system/cpu" / f"cpu{cpu}" / "topology"
                self.write_sensor(topology / "physical_package_id", f"{package}\n")
                self.write_sensor(topology / "core_id", f"{core}\n")
            self.assertEqual(monitor._linux_physical_core_count(sys_root), 2)

    def test_linux_network_and_power_collectors_use_64_bit_counters(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            sys_root = Path(folder) / "sysfs"
            adapter = sys_root / "class/net/enp4s0"
            (adapter / "device").mkdir(parents=True)
            self.write_sensor(adapter / "device/uevent", "DRIVER=igc\n")
            self.write_sensor(adapter / "operstate", "up\n")
            self.write_sensor(adapter / "carrier", "1\n")
            self.write_sensor(adapter / "ifindex", "7\n")
            self.write_sensor(adapter / "type", "1\n")
            self.write_sensor(adapter / "speed", "2500\n")
            self.write_sensor(adapter / "statistics/rx_bytes", str(9 * 1024**3 + 1))
            self.write_sensor(adapter / "statistics/tx_bytes", str(11 * 1024**3 + 2))
            virtual = sys_root / "class/net/tun0"
            self.write_sensor(virtual / "operstate", "up\n")
            self.write_sensor(virtual / "ifindex", "8\n")

            battery = sys_root / "class/power_supply/BAT0"
            self.write_sensor(battery / "type", "Battery\n")
            self.write_sensor(battery / "capacity", "83\n")
            self.write_sensor(battery / "status", "Charging\n")
            mains = sys_root / "class/power_supply/AC"
            self.write_sensor(mains / "type", "Mains\n")
            self.write_sensor(mains / "online", "1\n")

            interfaces = monitor._linux_network_interfaces(sys_root)
            self.assertEqual(len(interfaces), 1)
            interface = interfaces[0]
            self.assertEqual(interface.alias, "enp4s0")
            self.assertEqual(interface.luid, 7)
            self.assertEqual(interface.receive_link_bps, 2_500_000_000)
            self.assertEqual(interface.received_bytes, 9 * 1024**3 + 1)
            self.assertEqual(interface.sent_bytes, 11 * 1024**3 + 2)
            self.assertIn("igc driver", interface.description)
            self.assertEqual(monitor._linux_battery_info(sys_root), (83, True))

    def test_linux_multiple_batteries_use_capacity_weighted_percentage(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            sys_root = Path(folder) / "sysfs"
            for name, now, full, capacity in (
                ("BAT0", 900, 1000, 90),
                ("BAT1", 500, 2000, 25),
            ):
                battery = sys_root / "class/power_supply" / name
                self.write_sensor(battery / "type", "Battery\n")
                self.write_sensor(battery / "energy_now", f"{now}\n")
                self.write_sensor(battery / "energy_full", f"{full}\n")
                self.write_sensor(battery / "capacity", f"{capacity}\n")
                self.write_sensor(battery / "status", "Discharging\n")
            self.assertEqual(monitor._linux_battery_info(sys_root), (47, False))

    def test_linux_mount_parser_and_df_percentage_semantics(self):
        mountinfo = (
            "24 1 0:20 / / rw - overlay overlay rw\n"
            "25 24 8:1 / /mnt/data rw - ext4 /dev/sda1 rw\n"
            "26 24 8:2 / /media/My\\040Drive rw - exfat /dev/sdb1 rw\n"
            "27 24 0:50 / /mnt/server rw - nfs server:/share rw\n"
        )
        self.assertEqual(
            monitor._parse_linux_mountinfo(mountinfo),
            ("/mnt/data", "/media/My Drive"),
        )
        usage = SimpleNamespace(total=100, used=60, free=30)
        self.assertEqual(monitor._disk_used_percent(usage, linux=True), 66.7)
        self.assertEqual(monitor._disk_used_percent(usage, linux=False), 70.0)

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            proc_root = Path(folder) / "procfs"
            self.write_sensor(proc_root / "self/mountinfo", mountinfo)
            roots = monitor._linux_drive_roots(proc_root)
            self.assertEqual(roots[0], "/")
            self.assertIn("/mnt/data", roots)

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
