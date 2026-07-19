from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace

try:
    from hardware_monitor.sensors import (
        CapabilityState, LibreHardwareMonitorJsonProvider, LinuxHwmonProvider,
        NvidiaSmiProvider, ProviderResult, SensorHub, SensorKind,
        parse_hwmon_chip, parse_lhm_json, parse_nvidia_smi_csv,
    )
except ImportError:
    from v4_sensors import (
        CapabilityState, LibreHardwareMonitorJsonProvider, LinuxHwmonProvider,
        NvidiaSmiProvider, ProviderResult, SensorHub, SensorKind,
        parse_hwmon_chip, parse_lhm_json, parse_nvidia_smi_csv,
    )


class SensorTests(unittest.TestCase):
    def test_hwmon_parser_converts_units_and_keeps_threshold_flags(self) -> None:
        readings = parse_hwmon_chip(
            "coretemp",
            "/devices/pci0000/cpu0",
            {
                "temp1_input": "42500",
                "temp1_label": "Package id 0",
                "temp1_max": "90000",
                "temp1_crit": "100000",
                "temp1_alarm": "1",
                "fan2_input": "1432",
                "power1_input": "65500000",
                "in1_input": "1210",
                "temp9_input": "not-a-number",
            },
        )
        self.assertEqual(len(readings), 4)
        temperature = next(item for item in readings if item.kind is SensorKind.TEMPERATURE)
        self.assertEqual(temperature.value, 42.5)
        self.assertEqual(temperature.maximum, 90.0)
        self.assertEqual(temperature.critical, 100.0)
        self.assertTrue(temperature.alarm)
        self.assertEqual(next(item.value for item in readings if item.kind is SensorKind.POWER), 65.5)
        self.assertEqual(next(item.value for item in readings if item.kind is SensorKind.VOLTAGE), 1.21)
        repeated = parse_hwmon_chip("coretemp", "/devices/pci0000/cpu0", {"temp1_input": "43000"})
        self.assertEqual(temperature.sensor_id, repeated[0].sensor_id)

    def test_linux_provider_reports_platform_and_missing_states(self) -> None:
        unsupported = LinuxHwmonProvider(platform_name="win32").sample()
        self.assertEqual(unsupported.state, CapabilityState.UNSUPPORTED)
        missing = LinuxHwmonProvider("definitely-not-a-hwmon-directory", platform_name="linux").sample()
        self.assertEqual(missing.state, CapabilityState.MISSING)

    def test_lhm_parser_handles_nested_tree_and_numeric_values(self) -> None:
        payload = {
            "Text": "Sensor",
            "Children": [
                {
                    "Text": "My PC",
                    "Children": [
                        {
                            "Text": "CPU",
                            "Children": [
                                {"Text": "CPU Package", "SensorType": "Temperature", "Value": "51.5 °C", "Min": "32 °C", "Max": "72 °C"},
                                {"Name": "CPU Total", "Type": "Load", "Value": 17.25, "Unit": "%"},
                            ],
                        }
                    ],
                }
            ],
        }
        readings = parse_lhm_json(payload)
        self.assertEqual(len(readings), 2)
        temperature = readings[0]
        self.assertEqual(temperature.hardware, "My PC / CPU")
        self.assertEqual(temperature.value, 51.5)
        self.assertEqual(temperature.minimum, 32.0)
        self.assertEqual(temperature.maximum, 72.0)

    def test_lhm_provider_allows_only_loopback_and_closes_response(self) -> None:
        denied = LibreHardwareMonitorJsonProvider(
            "http://example.com/data.json", platform_name="win32"
        ).sample()
        self.assertEqual(denied.state, CapabilityState.DENIED)

        class Response:
            closed = False

            def read(self, _limit: int) -> bytes:
                return b'{"Children":[{"Text":"GPU","Children":[{"Text":"Core","SensorType":"Temperature","Value":"40 C"}]}]}'

            def close(self) -> None:
                self.closed = True

        response = Response()
        provider = LibreHardwareMonitorJsonProvider(
            platform_name="win32", opener=lambda *_args, **_kwargs: response
        )
        result = provider.sample()
        self.assertEqual(result.state, CapabilityState.AVAILABLE)
        self.assertEqual(len(result.readings), 1)
        self.assertTrue(response.closed)

    def test_nvidia_csv_parser_skips_unavailable_fields_and_computes_memory(self) -> None:
        readings = parse_nvidia_smi_csv(
            "GPU-abc, NVIDIA RTX Test, 53, N/A, 88.25, 41, 2048, 8192\n"
        )
        labels = {reading.label: reading for reading in readings}
        self.assertNotIn("Fan speed", labels)
        self.assertEqual(labels["GPU temperature"].value, 53.0)
        self.assertEqual(labels["GPU memory used"].value, 25.0)

    def test_nvidia_runner_has_safe_bounded_arguments_and_errors_are_states(self) -> None:
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout="GPU-1, RTX, 50, 30, 70, 20, 1000, 4000\n",
                stderr="",
            )

        result = NvidiaSmiProvider(
            executable="nvidia-smi", timeout=2, run=run
        ).sample()
        self.assertEqual(result.state, CapabilityState.AVAILABLE)
        arguments, kwargs = calls[0]
        self.assertIsInstance(arguments, list)
        self.assertEqual(arguments[0], "nvidia-smi")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 2)

        timeout = NvidiaSmiProvider(
            executable="nvidia-smi",
            run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("nvidia-smi", 1)
            ),
        ).sample()
        self.assertEqual(timeout.state, CapabilityState.ERROR)
        self.assertEqual(NvidiaSmiProvider(which=lambda _name: None).sample().state, CapabilityState.MISSING)

    def test_sensor_hub_isolates_provider_failure(self) -> None:
        class Broken:
            name = "Broken"

            def sample(self):
                raise RuntimeError("boom")

            def close(self):
                raise RuntimeError("also boom")

        class Empty:
            name = "Empty"

            def sample(self):
                return ProviderResult("Empty", CapabilityState.AVAILABLE)

            def close(self):
                return None

        hub = SensorHub((Broken(), Empty()))
        snapshot = hub.sample()
        self.assertEqual(snapshot.providers[0].state, CapabilityState.ERROR)
        self.assertEqual(snapshot.providers[1].state, CapabilityState.AVAILABLE)
        hub.close()


if __name__ == "__main__":
    unittest.main()
