"""Read-only SMART/NVMe health support backed by smartctl JSON.

``smartctl`` is optional and discovered lazily.  This module never starts a
self-test, changes drive settings, or requests serial numbers.  Every process
uses an argument list, ``shell=False``, a deadline, and injectable execution so
the parsers and command policy can be tested without touching a real drive.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


class SmartCapability(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    SLEEPING = "sleeping"
    ERROR = "error"


class HealthLevel(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SmartctlIssue:
    bit: int
    summary: str
    severe: bool


@dataclass(frozen=True)
class SmartDeviceDescriptor:
    name: str
    device_type: str = ""
    protocol: str = ""
    info_name: str = ""


@dataclass(frozen=True)
class SmartDeviceHealth:
    device: SmartDeviceDescriptor
    capability: SmartCapability
    health: HealthLevel = HealthLevel.UNKNOWN
    model: str = ""
    firmware: str = ""
    capacity_bytes: int | None = None
    temperature_c: float | None = None
    power_on_hours: int | None = None
    percentage_used: int | None = None
    available_spare: int | None = None
    available_spare_threshold: int | None = None
    unsafe_shutdowns: int | None = None
    media_errors: int | None = None
    data_units_read: int | None = None
    data_units_written: int | None = None
    issues: tuple[SmartctlIssue, ...] = ()
    messages: tuple[str, ...] = ()
    detail: str = ""
    captured_at: float = 0.0


@dataclass(frozen=True)
class SmartScanResult:
    capability: SmartCapability
    devices: tuple[SmartDeviceDescriptor, ...] = ()
    issues: tuple[SmartctlIssue, ...] = ()
    detail: str = ""


_EXIT_BITS: dict[int, tuple[str, bool]] = {
    0: ("smartctl command line could not be parsed", True),
    1: ("device could not be opened or identified", True),
    2: ("a SMART command or checksum failed", True),
    3: ("the drive reports imminent failure", True),
    4: ("a prefail attribute is below threshold now", True),
    5: ("a prefail attribute was below threshold previously", False),
    6: ("the device error log contains records", False),
    7: ("the self-test log contains errors", False),
}


def decode_smartctl_exit_status(status: object) -> tuple[SmartctlIssue, ...]:
    try:
        value = int(status) & 0xFF
    except (TypeError, ValueError, OverflowError):
        value = 0
    return tuple(
        SmartctlIssue(bit, summary, severe)
        for bit, (summary, severe) in _EXIT_BITS.items()
        if value & (1 << bit)
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested(payload: Mapping[str, Any], *path: str) -> object | None:
    value: object = payload
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    try:
        text = str(value).strip().replace(",", "")
        return int(text, 10)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str:
    return str(value or "").strip("\x00\r\n ")


def _messages(payload: Mapping[str, Any]) -> tuple[str, ...]:
    smartctl = _mapping(payload.get("smartctl"))
    raw_messages = smartctl.get("messages", ())
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return ()
    results: list[str] = []
    for item in raw_messages:
        if isinstance(item, Mapping):
            text = _text(item.get("string", item.get("message")))
        else:
            text = _text(item)
        if text:
            results.append(text[:500])
    return tuple(results)


def _safe_device_name(value: object) -> str | None:
    name = _text(value)
    if not name or len(name) > 1_024 or name.startswith("-") or "\x00" in name:
        return None
    return name


def _safe_device_type(value: object) -> str:
    device_type = _text(value)
    if (
        not device_type
        or len(device_type) > 80
        or device_type.startswith("-")
        or "\x00" in device_type
        or any(character.isspace() for character in device_type)
    ):
        return ""
    return device_type


def parse_smart_scan_json(payload: object) -> tuple[SmartDeviceDescriptor, ...]:
    """Parse and deduplicate the ``smartctl --scan --json`` device list."""

    root = _mapping(payload)
    devices = root.get("devices", ())
    if not isinstance(devices, Sequence) or isinstance(devices, (str, bytes)):
        return ()
    results: list[SmartDeviceDescriptor] = []
    seen: set[tuple[str, str]] = set()
    for item in devices:
        device = _mapping(item)
        name = _safe_device_name(device.get("name"))
        if name is None:
            continue
        raw_device_type = _text(device.get("type"))
        device_type = _safe_device_type(raw_device_type)
        if raw_device_type and not device_type:
            continue
        key = (name.casefold(), device_type.casefold())
        if key in seen:
            continue
        seen.add(key)
        results.append(
            SmartDeviceDescriptor(
                name=name,
                device_type=device_type,
                protocol=_text(device.get("protocol")),
                info_name=_text(device.get("info_name", device.get("info"))),
            )
        )
    return tuple(results)


def parse_smart_health_json(
    payload: object,
    device: SmartDeviceDescriptor,
    *,
    exit_status: int = 0,
    captured_at: float = 0.0,
) -> SmartDeviceHealth:
    """Normalize ATA and NVMe health JSON without retaining serial numbers."""

    root = _mapping(payload)
    issues = decode_smartctl_exit_status(exit_status)
    messages = _messages(root)
    message_text = " ".join(messages).casefold()
    smart_support = _mapping(root.get("smart_support"))

    if any(token in message_text for token in ("permission denied", "access denied", "must be root")):
        capability = SmartCapability.DENIED
        detail = "Permission was denied while reading SMART data."
    elif any(token in message_text for token in ("standby mode", "sleep mode", "device is asleep")):
        capability = SmartCapability.SLEEPING
        detail = "Drive is sleeping; it was not woken for this check."
    elif smart_support.get("available") is False or "smart support is: unavailable" in message_text:
        capability = SmartCapability.UNSUPPORTED
        detail = "This device does not expose SMART health data."
    elif any(issue.bit in {0, 1, 2} for issue in issues):
        capability = SmartCapability.ERROR
        detail = "smartctl could not complete the health query."
    else:
        capability = SmartCapability.AVAILABLE
        detail = ""

    passed_value = _nested(root, "smart_status", "passed")
    nvme = _mapping(root.get("nvme_smart_health_information_log"))
    critical_warning = _integer(nvme.get("critical_warning")) or 0
    percentage_used = _integer(nvme.get("percentage_used"))
    spare = _integer(nvme.get("available_spare"))
    spare_threshold = _integer(nvme.get("available_spare_threshold"))

    if capability is not SmartCapability.AVAILABLE:
        health = HealthLevel.UNKNOWN
    elif passed_value is False or any(issue.bit in {3, 4} for issue in issues) or critical_warning:
        health = HealthLevel.FAILED
    elif (
        any(issue.bit in {5, 6, 7} for issue in issues)
        or (percentage_used is not None and percentage_used >= 100)
        or (
            spare is not None
            and spare_threshold is not None
            and spare <= spare_threshold
        )
    ):
        health = HealthLevel.WARNING
    elif passed_value is True or nvme:
        health = HealthLevel.PASSED
    else:
        health = HealthLevel.UNKNOWN

    temperature = _number(_nested(root, "temperature", "current"))
    if temperature is None:
        temperature = _number(nvme.get("temperature"))
    power_on_hours = _integer(_nested(root, "power_on_time", "hours"))
    if power_on_hours is None:
        power_on_hours = _integer(nvme.get("power_on_hours"))

    model = _text(root.get("model_name", root.get("product", root.get("device", {}).get("name") if isinstance(root.get("device"), Mapping) else "")))
    firmware = _text(root.get("firmware_version", root.get("revision")))

    return SmartDeviceHealth(
        device=device,
        capability=capability,
        health=health,
        model=model,
        firmware=firmware,
        capacity_bytes=_integer(_nested(root, "user_capacity", "bytes")),
        temperature_c=temperature,
        power_on_hours=power_on_hours,
        percentage_used=percentage_used,
        available_spare=spare,
        available_spare_threshold=spare_threshold,
        unsafe_shutdowns=_integer(nvme.get("unsafe_shutdowns")),
        media_errors=_integer(nvme.get("media_errors")),
        data_units_read=_integer(nvme.get("data_units_read")),
        data_units_written=_integer(nvme.get("data_units_written")),
        issues=issues,
        messages=messages,
        detail=detail,
        captured_at=captured_at,
    )


class SmartctlRunner:
    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        timeout: float = 6.0,
        run: Callable[..., Any] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.executable = str(executable) if executable is not None else None
        self.timeout = max(0.5, min(float(timeout), 30.0))
        self.run = run
        self.which = which
        self.clock = clock

    def _command(self) -> str | None:
        return self.executable or self.which("smartctl")

    def _execute(self, command: str, arguments: Iterable[str]) -> Any:
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
        return self.run([command, *arguments], **kwargs)

    @staticmethod
    def _load_output(completed: Any) -> Mapping[str, Any]:
        stdout = _text(getattr(completed, "stdout", ""))
        if not stdout:
            return {}
        payload = json.loads(stdout)
        return _mapping(payload)

    @staticmethod
    def _process_error(exc: BaseException) -> tuple[SmartCapability, str]:
        if isinstance(exc, FileNotFoundError):
            return SmartCapability.MISSING, "smartctl was not found."
        if isinstance(exc, PermissionError):
            return SmartCapability.DENIED, "smartctl could not be executed."
        if isinstance(exc, subprocess.TimeoutExpired):
            return SmartCapability.ERROR, "smartctl timed out."
        return SmartCapability.ERROR, f"smartctl failed: {exc}"

    def scan(self) -> SmartScanResult:
        command = self._command()
        if not command:
            return SmartScanResult(
                SmartCapability.MISSING,
                detail="smartctl is not installed or not on PATH.",
            )
        try:
            completed = self._execute(command, ("--scan", "--json"))
            payload = self._load_output(completed)
        except (OSError, subprocess.SubprocessError) as exc:
            state, detail = self._process_error(exc)
            return SmartScanResult(state, detail=detail)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            return SmartScanResult(
                SmartCapability.ERROR, detail=f"Invalid smartctl JSON: {exc}"
            )
        issues = decode_smartctl_exit_status(getattr(completed, "returncode", 0))
        messages = " ".join(_messages(payload)).casefold()
        if "permission denied" in messages or "access denied" in messages:
            state = SmartCapability.DENIED
        elif any(issue.bit in {0, 1, 2} for issue in issues):
            state = SmartCapability.ERROR
        else:
            state = SmartCapability.AVAILABLE
        devices = parse_smart_scan_json(payload)
        detail = "No SMART devices were discovered." if state is SmartCapability.AVAILABLE and not devices else ""
        return SmartScanResult(state, devices, issues, detail)

    def poll(self, device: SmartDeviceDescriptor) -> SmartDeviceHealth:
        captured = self.clock()
        name = _safe_device_name(device.name)
        device_type = _safe_device_type(device.device_type)
        if name is None or (device.device_type and not device_type):
            return SmartDeviceHealth(
                device,
                SmartCapability.DENIED,
                detail="Unsafe SMART device descriptor was rejected.",
                captured_at=captured,
            )
        command = self._command()
        if not command:
            return SmartDeviceHealth(
                device,
                SmartCapability.MISSING,
                detail="smartctl is not installed or not on PATH.",
                captured_at=captured,
            )
        arguments = [
            "--json",
            "--quietmode=noserial",
            "--info",
            "--health",
            "--attributes",
            "--nocheck=standby,0",
        ]
        if device_type:
            arguments.append(f"--device={device_type}")
        arguments.append(name)
        try:
            completed = self._execute(command, arguments)
            payload = self._load_output(completed)
        except (OSError, subprocess.SubprocessError) as exc:
            state, detail = self._process_error(exc)
            return SmartDeviceHealth(device, state, detail=detail, captured_at=captured)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            return SmartDeviceHealth(
                device,
                SmartCapability.ERROR,
                detail=f"Invalid smartctl JSON: {exc}",
                captured_at=captured,
            )
        return parse_smart_health_json(
            payload,
            device,
            exit_status=getattr(completed, "returncode", 0),
            captured_at=captured,
        )

    def poll_all(
        self,
        devices: Iterable[SmartDeviceDescriptor],
        *,
        limit: int = 64,
    ) -> tuple[SmartDeviceHealth, ...]:
        bounded_limit = max(0, min(int(limit), 256))
        return tuple(self.poll(device) for index, device in enumerate(devices) if index < bounded_limit)


__all__ = [
    "HealthLevel",
    "SmartCapability",
    "SmartDeviceDescriptor",
    "SmartDeviceHealth",
    "SmartScanResult",
    "SmartctlIssue",
    "SmartctlRunner",
    "decode_smartctl_exit_status",
    "parse_smart_health_json",
    "parse_smart_scan_json",
]
