"""Read-only, cross-platform optional hardware sensors for NEXUS v4.

The providers in this module deliberately fail *closed*: optional sensor tools
may be absent, denied, asleep, or temporarily broken without taking the main
hardware monitor down.  Linux hwmon is read directly, LibreHardwareMonitor is
accepted only from a loopback HTTP endpoint, and ``nvidia-smi`` is used as a
safe subprocess fallback for NVIDIA telemetry.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import math
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class CapabilityState(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class SensorKind(str, Enum):
    TEMPERATURE = "temperature"
    FAN = "fan"
    POWER = "power"
    VOLTAGE = "voltage"
    LOAD = "load"


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    source: str
    hardware: str
    label: str
    kind: SensorKind
    value: float
    unit: str
    minimum: float | None = None
    maximum: float | None = None
    critical: float | None = None
    alarm: bool = False
    fault: bool = False


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    state: CapabilityState
    readings: tuple[SensorReading, ...] = ()
    detail: str = ""
    captured_at: float = 0.0


@dataclass(frozen=True)
class SensorSnapshot:
    readings: tuple[SensorReading, ...]
    providers: tuple[ProviderResult, ...]
    captured_at: float


class SensorProvider(Protocol):
    name: str

    def sample(self) -> ProviderResult:
        ...

    def close(self) -> None:
        ...


def _stable_id(*parts: str) -> str:
    normalized = "\x1f".join(part.strip().casefold() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:24]


def _finite(value: object, divisor: float = 1.0) -> float | None:
    try:
        number = float(value) / divisor
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


_HWMON_INPUT = re.compile(r"^(temp|fan|power|in)(\d+)_input$")
_HWMON_META: dict[str, tuple[SensorKind, float, str, str]] = {
    "temp": (SensorKind.TEMPERATURE, 1_000.0, "°C", "Temperature"),
    "fan": (SensorKind.FAN, 1.0, "RPM", "Fan"),
    "power": (SensorKind.POWER, 1_000_000.0, "W", "Power"),
    "in": (SensorKind.VOLTAGE, 1_000.0, "V", "Voltage"),
}


def parse_hwmon_chip(
    chip_name: str,
    canonical_device: str,
    files: Mapping[str, str],
) -> tuple[SensorReading, ...]:
    """Parse one Linux hwmon chip from an in-memory filename/value mapping."""

    readings: list[SensorReading] = []
    for filename in sorted(files):
        match = _HWMON_INPUT.fullmatch(filename)
        if not match:
            continue
        prefix, index = match.groups()
        kind, divisor, unit, default_label = _HWMON_META[prefix]
        value = _finite(files.get(filename), divisor)
        if value is None:
            continue
        base = f"{prefix}{index}"
        label = files.get(f"{base}_label", "").strip() or f"{default_label} {index}"
        minimum = _finite(files.get(f"{base}_min"), divisor)
        maximum = _finite(files.get(f"{base}_max"), divisor)
        critical = _finite(files.get(f"{base}_crit"), divisor)
        alarm = str(files.get(f"{base}_alarm", "0")).strip() not in {"", "0"}
        fault = str(files.get(f"{base}_fault", "0")).strip() not in {"", "0"}
        readings.append(
            SensorReading(
                sensor_id=_stable_id("linux-hwmon", canonical_device, base),
                source="linux-hwmon",
                hardware=chip_name,
                label=label,
                kind=kind,
                value=value,
                unit=unit,
                minimum=minimum,
                maximum=maximum,
                critical=critical,
                alarm=alarm,
                fault=fault,
            )
        )
    return tuple(readings)


def parse_linux_hwmon(root: str | Path = "/sys/class/hwmon") -> tuple[SensorReading, ...]:
    """Read and parse Linux hwmon.  No control or ``pwm*`` file is opened."""

    root_path = Path(root)
    readings: list[SensorReading] = []
    for chip in sorted(root_path.iterdir(), key=lambda item: item.name):
        if not chip.is_dir():
            continue
        try:
            chip_name = (chip / "name").read_text(
                encoding="utf-8", errors="replace"
            ).strip("\x00\r\n ") or chip.name
        except (FileNotFoundError, PermissionError, OSError):
            chip_name = chip.name

        device = chip / "device"
        try:
            canonical_device = str(device.resolve(strict=True))
        except (FileNotFoundError, PermissionError, OSError):
            canonical_device = str(chip.resolve(strict=False))

        files: dict[str, str] = {}
        try:
            names = tuple(chip.iterdir())
        except PermissionError:
            continue
        for path in names:
            name = path.name
            match = _HWMON_INPUT.fullmatch(name)
            if not match:
                continue
            prefix, index = match.groups()
            base = f"{prefix}{index}"
            for suffix in ("input", "label", "min", "max", "crit", "alarm", "fault"):
                candidate = chip / f"{base}_{suffix}"
                try:
                    files[candidate.name] = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip("\x00\r\n ")
                except (FileNotFoundError, PermissionError, OSError):
                    continue
        readings.extend(parse_hwmon_chip(chip_name, canonical_device, files))
    return tuple(readings)


class LinuxHwmonProvider:
    name = "Linux hwmon"

    def __init__(
        self,
        root: str | Path = "/sys/class/hwmon",
        *,
        platform_name: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.platform_name = platform_name or sys.platform
        self.clock = clock

    def sample(self) -> ProviderResult:
        captured = self.clock()
        if not self.platform_name.startswith("linux"):
            return ProviderResult(
                self.name,
                CapabilityState.UNSUPPORTED,
                detail="Linux hwmon is only available on Linux.",
                captured_at=captured,
            )
        if not self.root.exists():
            return ProviderResult(
                self.name,
                CapabilityState.MISSING,
                detail=f"{self.root} is not present.",
                captured_at=captured,
            )
        try:
            readings = parse_linux_hwmon(self.root)
        except PermissionError:
            return ProviderResult(
                self.name,
                CapabilityState.DENIED,
                detail="Permission denied while reading hwmon.",
                captured_at=captured,
            )
        except OSError as exc:
            return ProviderResult(
                self.name,
                CapabilityState.ERROR,
                detail=f"hwmon read failed: {exc}",
                captured_at=captured,
            )
        return ProviderResult(
            self.name,
            CapabilityState.AVAILABLE,
            readings,
            "No readable sensors were exposed." if not readings else "",
            captured,
        )

    def close(self) -> None:
        return None


_VALUE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _value_and_unit(value: object, explicit_unit: object = "") -> tuple[float, str] | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite(value)
        return (number, str(explicit_unit).strip()) if number is not None else None
    text = str(value or "").strip()
    match = _VALUE.search(text)
    if not match:
        return None
    token = match.group(0).replace(",", ".")
    number = _finite(token)
    if number is None:
        return None
    unit = str(explicit_unit or text[match.end() :]).strip()
    unit = unit.replace("º", "°")
    return number, unit


def _kind_for_sensor(sensor_type: object, unit: str) -> SensorKind | None:
    token = str(sensor_type or "").casefold()
    normalized_unit = unit.casefold()
    if "temp" in token or "°c" in normalized_unit or "°f" in normalized_unit:
        return SensorKind.TEMPERATURE
    if "fan" in token or normalized_unit in {"rpm", "r/min"}:
        return SensorKind.FAN
    if "power" in token or normalized_unit == "w":
        return SensorKind.POWER
    if "voltage" in token or normalized_unit == "v":
        return SensorKind.VOLTAGE
    if "load" in token or normalized_unit == "%":
        return SensorKind.LOAD
    return None


def parse_lhm_json(payload: object) -> tuple[SensorReading, ...]:
    """Parse both tree and list shaped LibreHardwareMonitor JSON payloads."""

    readings: list[SensorReading] = []

    def walk(node: object, hardware_path: tuple[str, ...]) -> None:
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for child in node:
                walk(child, hardware_path)
            return
        if not isinstance(node, Mapping):
            return

        label = str(
            node.get("Text")
            or node.get("Name")
            or node.get("name")
            or node.get("Label")
            or ""
        ).strip()
        children = node.get("Children", node.get("children", node.get("Sensors")))
        raw_value = node.get("Value", node.get("value"))
        parsed = _value_and_unit(raw_value, node.get("Unit", node.get("unit", "")))
        sensor_type = node.get("SensorType", node.get("Type", node.get("type", "")))

        if parsed is not None:
            value, unit = parsed
            kind = _kind_for_sensor(sensor_type, unit)
            if kind is not None:
                hardware = " / ".join(hardware_path) or "LibreHardwareMonitor"
                identifier = str(
                    node.get("SensorId")
                    or node.get("Identifier")
                    or node.get("id")
                    or label
                )
                minimum = _value_and_unit(node.get("Min", node.get("min")), unit)
                maximum = _value_and_unit(node.get("Max", node.get("max")), unit)
                readings.append(
                    SensorReading(
                        sensor_id=_stable_id("lhm", hardware, identifier),
                        source="librehardwaremonitor",
                        hardware=hardware,
                        label=label or kind.value.title(),
                        kind=kind,
                        value=value,
                        unit=unit,
                        minimum=minimum[0] if minimum else None,
                        maximum=maximum[0] if maximum else None,
                    )
                )

        next_path = hardware_path
        if label and parsed is None and label.casefold() not in {"sensor", "sensors"}:
            next_path = (*hardware_path, label)
        if children is not None:
            walk(children, next_path)

    walk(payload, ())
    return tuple(readings)


def _is_loopback_http(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if parsed.scheme.casefold() != "http" or not host or parsed.username or parsed.password:
            return False
        if host.casefold() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


class LibreHardwareMonitorJsonProvider:
    name = "LibreHardwareMonitor"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8085/data.json",
        *,
        timeout: float = 1.5,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        platform_name: str | None = None,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = max(0.1, min(float(timeout), 10.0))
        self.opener = opener
        self.clock = clock
        self.platform_name = platform_name or sys.platform
        self.max_bytes = max(1_024, min(int(max_bytes), 16 * 1024 * 1024))

    def sample(self) -> ProviderResult:
        captured = self.clock()
        if not self.platform_name.startswith("win"):
            return ProviderResult(
                self.name,
                CapabilityState.UNSUPPORTED,
                detail="LibreHardwareMonitor integration is intended for Windows.",
                captured_at=captured,
            )
        if not _is_loopback_http(self.endpoint):
            return ProviderResult(
                self.name,
                CapabilityState.DENIED,
                detail="Only a loopback HTTP LibreHardwareMonitor endpoint is allowed.",
                captured_at=captured,
            )
        request = urllib.request.Request(
            self.endpoint,
            headers={"Accept": "application/json", "User-Agent": "NEXUS-Hardware-Monitor/4"},
        )
        try:
            with closing(self.opener(request, timeout=self.timeout)) as response:
                body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise ValueError("response exceeded the size limit")
            payload = json.loads(body.decode("utf-8-sig"))
            readings = parse_lhm_json(payload)
        except urllib.error.HTTPError as exc:
            state = CapabilityState.DENIED if exc.code in {401, 403} else CapabilityState.ERROR
            return ProviderResult(
                self.name, state, detail=f"HTTP {exc.code} from LibreHardwareMonitor.", captured_at=captured
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return ProviderResult(
                self.name,
                CapabilityState.MISSING,
                detail="LibreHardwareMonitor web server is not reachable on loopback.",
                captured_at=captured,
            )
        except (json.JSONDecodeError, UnicodeError, ValueError, OSError) as exc:
            return ProviderResult(
                self.name,
                CapabilityState.ERROR,
                detail=f"LibreHardwareMonitor data could not be read: {exc}",
                captured_at=captured,
            )
        return ProviderResult(
            self.name,
            CapabilityState.AVAILABLE,
            readings,
            "Connected, but no supported sensors were reported." if not readings else "",
            captured,
        )

    def close(self) -> None:
        return None


NVIDIA_QUERY_FIELDS = (
    "uuid",
    "name",
    "temperature.gpu",
    "fan.speed",
    "power.draw",
    "utilization.gpu",
    "memory.used",
    "memory.total",
)


def parse_nvidia_smi_csv(text: str) -> tuple[SensorReading, ...]:
    """Parse ``nvidia-smi --format=csv,noheader,nounits`` output."""

    readings: list[SensorReading] = []
    for row_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(NVIDIA_QUERY_FIELDS):
            raise ValueError(f"NVIDIA CSV row {row_number} has {len(row)} fields")
        values = [cell.strip() for cell in row]
        gpu_id = values[0] or f"gpu-{row_number}"
        hardware = values[1] or "NVIDIA GPU"

        def add(index: int, label: str, kind: SensorKind, unit: str) -> None:
            value = _finite(values[index])
            if value is None:
                return
            readings.append(
                SensorReading(
                    sensor_id=_stable_id("nvidia-smi", gpu_id, label),
                    source="nvidia-smi",
                    hardware=hardware,
                    label=label,
                    kind=kind,
                    value=value,
                    unit=unit,
                )
            )

        add(2, "GPU temperature", SensorKind.TEMPERATURE, "°C")
        add(3, "Fan speed", SensorKind.FAN, "%")
        add(4, "GPU power", SensorKind.POWER, "W")
        add(5, "GPU load", SensorKind.LOAD, "%")
        used, total = _finite(values[6]), _finite(values[7])
        if used is not None and total is not None and total > 0:
            readings.append(
                SensorReading(
                    sensor_id=_stable_id("nvidia-smi", gpu_id, "memory-load"),
                    source="nvidia-smi",
                    hardware=hardware,
                    label="GPU memory used",
                    kind=SensorKind.LOAD,
                    value=max(0.0, min(100.0, used / total * 100.0)),
                    unit="%",
                )
            )
    return tuple(readings)


class NvidiaSmiProvider:
    name = "NVIDIA SMI"

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        timeout: float = 3.0,
        run: Callable[..., Any] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.executable = str(executable) if executable is not None else None
        self.timeout = max(0.2, min(float(timeout), 15.0))
        self.run = run
        self.which = which
        self.clock = clock

    def _command(self) -> str | None:
        return self.executable or self.which("nvidia-smi")

    def sample(self) -> ProviderResult:
        captured = self.clock()
        command = self._command()
        if not command:
            return ProviderResult(
                self.name,
                CapabilityState.MISSING,
                detail="nvidia-smi is not installed or not on PATH.",
                captured_at=captured,
            )
        arguments = [
            command,
            "--query-gpu=" + ",".join(NVIDIA_QUERY_FIELDS),
            "--format=csv,noheader,nounits",
        ]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout,
            "check": False,
            "shell": False,
        }
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = self.run(arguments, **kwargs)
        except FileNotFoundError:
            return ProviderResult(
                self.name, CapabilityState.MISSING, detail="nvidia-smi was not found.", captured_at=captured
            )
        except PermissionError:
            return ProviderResult(
                self.name, CapabilityState.DENIED, detail="nvidia-smi could not be executed.", captured_at=captured
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                self.name, CapabilityState.ERROR, detail="nvidia-smi timed out.", captured_at=captured
            )
        except OSError as exc:
            return ProviderResult(
                self.name, CapabilityState.ERROR, detail=f"nvidia-smi failed: {exc}", captured_at=captured
            )
        if int(getattr(completed, "returncode", 1)) != 0:
            detail = str(getattr(completed, "stderr", "")).strip().splitlines()
            reason = detail[0][:240] if detail else "unknown error"
            return ProviderResult(
                self.name,
                CapabilityState.ERROR,
                detail=f"nvidia-smi returned an error: {reason}",
                captured_at=captured,
            )
        try:
            readings = parse_nvidia_smi_csv(str(getattr(completed, "stdout", "")))
        except ValueError as exc:
            return ProviderResult(
                self.name, CapabilityState.ERROR, detail=str(exc), captured_at=captured
            )
        return ProviderResult(
            self.name,
            CapabilityState.AVAILABLE,
            readings,
            "No supported NVIDIA sensor values were returned." if not readings else "",
            captured,
        )

    def close(self) -> None:
        return None


class SensorHub:
    """Samples providers independently and merges readings without duplicates."""

    def __init__(
        self,
        providers: Iterable[SensorProvider],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.providers = tuple(providers)
        self.clock = clock

    def sample(self) -> SensorSnapshot:
        results: list[ProviderResult] = []
        readings: list[SensorReading] = []
        seen: set[tuple[str, str, str]] = set()
        for provider in self.providers:
            try:
                result = provider.sample()
            except Exception as exc:  # Provider boundary: one plugin cannot break the UI.
                result = ProviderResult(
                    getattr(provider, "name", type(provider).__name__),
                    CapabilityState.ERROR,
                    detail=f"Unexpected provider failure: {exc}",
                    captured_at=self.clock(),
                )
            results.append(result)
            for reading in result.readings:
                key = (
                    reading.hardware.casefold(),
                    reading.label.casefold(),
                    reading.kind.value,
                )
                if key not in seen:
                    seen.add(key)
                    readings.append(reading)
        return SensorSnapshot(tuple(readings), tuple(results), self.clock())

    def close(self) -> None:
        for provider in self.providers:
            try:
                provider.close()
            except Exception:
                continue


def default_sensor_providers(
    platform_name: str | None = None,
) -> tuple[SensorProvider, ...]:
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        return (
            LibreHardwareMonitorJsonProvider(platform_name=platform_name),
            NvidiaSmiProvider(),
        )
    if platform_name.startswith("linux"):
        return (LinuxHwmonProvider(platform_name=platform_name), NvidiaSmiProvider())
    return (NvidiaSmiProvider(),)


__all__ = [
    "CapabilityState",
    "LibreHardwareMonitorJsonProvider",
    "LinuxHwmonProvider",
    "NVIDIA_QUERY_FIELDS",
    "NvidiaSmiProvider",
    "ProviderResult",
    "SensorHub",
    "SensorKind",
    "SensorReading",
    "SensorSnapshot",
    "default_sensor_providers",
    "parse_hwmon_chip",
    "parse_lhm_json",
    "parse_linux_hwmon",
    "parse_nvidia_smi_csv",
]
