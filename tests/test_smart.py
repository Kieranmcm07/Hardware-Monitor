from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace

try:
    from hardware_monitor.smart import (
        HealthLevel, SmartCapability, SmartDeviceDescriptor, SmartctlRunner,
        decode_smartctl_exit_status, parse_smart_health_json,
        parse_smart_scan_json,
    )
except ImportError:
    from v4_smart import (
        HealthLevel, SmartCapability, SmartDeviceDescriptor, SmartctlRunner,
        decode_smartctl_exit_status, parse_smart_health_json,
        parse_smart_scan_json,
    )


class SmartTests(unittest.TestCase):
    def test_exit_status_is_a_bitmask(self) -> None:
        issues = decode_smartctl_exit_status((1 << 3) | (1 << 6))
        self.assertEqual([issue.bit for issue in issues], [3, 6])
        self.assertTrue(issues[0].severe)
        self.assertFalse(issues[1].severe)

    def test_scan_deduplicates_and_rejects_option_like_devices(self) -> None:
        devices = parse_smart_scan_json(
            {
                "devices": [
                    {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
                    {"name": "/dev/sda", "type": "sat"},
                    {"name": "--all", "type": "sat"},
                    {"name": "/dev/nvme0", "type": "nvme bad"},
                ]
            }
        )
        self.assertEqual(devices, (SmartDeviceDescriptor("/dev/sda", "sat", "ATA", ""),))

    def test_nvme_parser_preserves_large_integers_and_omits_serial(self) -> None:
        huge = 12_345_678_901_234_567_890_123
        device = SmartDeviceDescriptor("/dev/nvme0", "nvme")
        health = parse_smart_health_json(
            {
                "model_name": "Fast NVMe",
                "serial_number": "DO-NOT-STORE-ME",
                "firmware_version": "1.2.3",
                "user_capacity": {"bytes": huge},
                "smart_status": {"passed": True},
                "nvme_smart_health_information_log": {
                    "critical_warning": 0,
                    "temperature": 46,
                    "available_spare": 97,
                    "available_spare_threshold": 10,
                    "percentage_used": 4,
                    "power_on_hours": 809,
                    "unsafe_shutdowns": 2,
                    "media_errors": 0,
                    "data_units_read": huge,
                    "data_units_written": huge + 1,
                },
            },
            device,
            captured_at=123.0,
        )
        self.assertEqual(health.capability, SmartCapability.AVAILABLE)
        self.assertEqual(health.health, HealthLevel.PASSED)
        self.assertEqual(health.capacity_bytes, huge)
        self.assertEqual(health.data_units_read, huge)
        self.assertEqual(health.data_units_written, huge + 1)
        self.assertNotIn("serial", repr(health).casefold())

    def test_failure_warning_permission_and_sleep_are_distinct(self) -> None:
        device = SmartDeviceDescriptor("/dev/test")
        failed = parse_smart_health_json(
            {"smart_status": {"passed": False}}, device, exit_status=1 << 3
        )
        self.assertEqual(failed.health, HealthLevel.FAILED)
        warning = parse_smart_health_json(
            {"smart_status": {"passed": True}}, device, exit_status=1 << 6
        )
        self.assertEqual(warning.health, HealthLevel.WARNING)
        denied = parse_smart_health_json(
            {"smartctl": {"messages": [{"string": "Permission denied"}]}},
            device,
            exit_status=1 << 1,
        )
        self.assertEqual(denied.capability, SmartCapability.DENIED)
        sleeping = parse_smart_health_json(
            {"smartctl": {"messages": [{"string": "Device is in STANDBY mode"}]}},
            device,
            exit_status=1 << 2,
        )
        self.assertEqual(sleeping.capability, SmartCapability.SLEEPING)

    def test_runner_uses_read_only_no_serial_safe_arguments(self) -> None:
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"smart_status": {"passed": True}}),
                stderr="",
            )

        runner = SmartctlRunner(executable="smartctl", timeout=4, run=run)
        result = runner.poll(SmartDeviceDescriptor("/dev/sda", "sat"))
        self.assertEqual(result.health, HealthLevel.PASSED)
        arguments, kwargs = calls[0]
        self.assertEqual(arguments[0], "smartctl")
        self.assertIn("--quietmode=noserial", arguments)
        self.assertIn("--nocheck=standby,0", arguments)
        self.assertIn("--device=sat", arguments)
        self.assertEqual(arguments[-1], "/dev/sda")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 4)
        self.assertFalse(any("test" in argument.casefold() for argument in arguments))

    def test_scan_and_missing_timeout_invalid_device_return_states(self) -> None:
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout='{"devices":[{"name":"/dev/sda","type":"sat"}]}',
                stderr="",
            )

        scan = SmartctlRunner(executable="smartctl", run=run).scan()
        self.assertEqual(scan.capability, SmartCapability.AVAILABLE)
        self.assertEqual(calls[0][0], ["smartctl", "--scan", "--json"])
        self.assertEqual(SmartctlRunner(which=lambda _name: None).scan().capability, SmartCapability.MISSING)

        timeout = SmartctlRunner(
            executable="smartctl",
            run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("smartctl", 1)
            ),
        ).poll(SmartDeviceDescriptor("/dev/sda"))
        self.assertEqual(timeout.capability, SmartCapability.ERROR)
        invalid = SmartctlRunner(executable="smartctl", run=run).poll(
            SmartDeviceDescriptor("--scan")
        )
        self.assertEqual(invalid.capability, SmartCapability.DENIED)


if __name__ == "__main__":
    unittest.main()
