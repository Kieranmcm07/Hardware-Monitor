from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import shutil
import struct
import tempfile
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None


GIB = 1024**3


@dataclass(frozen=True)
class DriveInfo:
    name: str
    total_gib: float
    free_gib: float
    used_percent: float


@dataclass(frozen=True)
class HardwareInfo:
    cpu_name: str
    physical_cores: int | None
    logical_cpus: int
    cpu_max_mhz: int | None
    gpu_names: tuple[str, ...]
    motherboard: str
    bios_version: str
    installed_memory_gib: float | None
    architecture: str


@dataclass
class Snapshot:
    computer: str
    operating_system: str
    processor: str
    physical_cores: int | None
    logical_cpus: int
    cpu_usage_percent: float | None
    memory_installed_gib: float | None
    memory_total_gib: float | None
    memory_used_gib: float | None
    memory_available_gib: float | None
    memory_used_percent: float | None
    system_drive: str
    disk_total_gib: float
    disk_free_gib: float
    disk_used_percent: float
    battery_percent: int | None
    plugged_in: bool | None
    uptime_seconds: float | None
    captured_at: float
    drives: tuple[DriveInfo, ...]

    # Compatibility with the original CLI/tests.
    @property
    def memory_total_gb(self) -> float | None:
        return self.memory_total_gib

    @property
    def disk_total_gb(self) -> float:
        return self.disk_total_gib

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _registry_value(path: str, name: str) -> object | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _clean(value: object | None, fallback: str = "Unavailable") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _physical_core_count() -> int | None:
    if os.name != "nt":
        return None
    relation_processor_core = 0
    size = ctypes.c_ulong(0)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLogicalProcessorInformationEx(relation_processor_core, None, ctypes.byref(size))
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not kernel32.GetLogicalProcessorInformationEx(
        relation_processor_core, buffer, ctypes.byref(size)
    ):
        return None
    offset = 0
    cores = 0
    raw = buffer.raw
    while offset + 8 <= size.value:
        relationship, record_size = struct.unpack_from("II", raw, offset)
        if record_size < 8:
            break
        if relationship == relation_processor_core:
            cores += 1
        offset += record_size
    return cores or None


def _gpu_names() -> tuple[str, ...]:
    if os.name != "nt":
        return ()

    class DisplayDevice(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", ctypes.c_ulong),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    names: list[str] = []
    index = 0
    while True:
        device = DisplayDevice()
        device.cb = ctypes.sizeof(device)
        if not ctypes.windll.user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        name = _clean(device.DeviceString, "")
        # Ignore mirroring/remote pseudo-adapters and duplicates.
        if name and not device.StateFlags & 0x8 and name not in names:
            names.append(name)
        index += 1
    return tuple(names)


def _installed_memory_gib() -> float | None:
    if os.name != "nt":
        return None
    installed_kib = ctypes.c_ulonglong()
    if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(installed_kib)):
        return round(installed_kib.value / 1024**2, 2)
    return None


@lru_cache(maxsize=1)
def hardware_info() -> HardwareInfo:
    cpu_key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    bios_key = r"HARDWARE\DESCRIPTION\System\BIOS"
    cpu_name = _clean(
        _registry_value(cpu_key, "ProcessorNameString"),
        platform.processor() or platform.machine() or "Unknown processor",
    )
    mhz_value = _registry_value(cpu_key, "~MHz")
    try:
        max_mhz = int(mhz_value) if mhz_value is not None else None
    except (TypeError, ValueError):
        max_mhz = None
    board_maker = _clean(_registry_value(bios_key, "BaseBoardManufacturer"), "")
    board_model = _clean(_registry_value(bios_key, "BaseBoardProduct"), "")
    motherboard = " ".join(part for part in (board_maker, board_model) if part) or "Unavailable"
    bios = _clean(
        _registry_value(bios_key, "BIOSVersion")
        or _registry_value(bios_key, "SystemBiosVersion")
    )
    return HardwareInfo(
        cpu_name=cpu_name,
        physical_cores=_physical_core_count(),
        logical_cpus=os.cpu_count() or 1,
        cpu_max_mhz=max_mhz,
        gpu_names=_gpu_names(),
        motherboard=motherboard,
        bios_version=bios,
        installed_memory_gib=_installed_memory_gib(),
        architecture=platform.machine() or "Unknown",
    )


def _windows_cpu_times() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    idle = ctypes.c_ulonglong()
    kernel = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    # Kernel time includes idle time; total busy time is derived after sampling.
    return idle.value, kernel.value + user.value


def cpu_usage(sample_seconds: float = 0.25) -> float | None:
    first = _windows_cpu_times()
    if first is None:
        return None
    time.sleep(max(0.05, sample_seconds))
    second = _windows_cpu_times()
    if second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100 * (1 - idle_delta / total_delta))), 1)


def memory_info() -> tuple[float | None, float | None, float | None, float | None]:
    if os.name != "nt":
        return None, None, None, None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong),
            ("total_page", ctypes.c_ulonglong),
            ("avail_page", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_extended", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None, None, None
    total = status.total_phys / GIB
    available = status.avail_phys / GIB
    used = total - available
    percent = used / total * 100 if total else None
    return round(total, 2), round(used, 2), round(available, 2), round(percent, 1)


def battery_info() -> tuple[int | None, bool | None]:
    if os.name != "nt":
        return None, None

    class PowerStatus(ctypes.Structure):
        _fields_ = [
            ("ac_line", ctypes.c_ubyte),
            ("battery_flag", ctypes.c_ubyte),
            ("battery_percent", ctypes.c_ubyte),
            ("reserved", ctypes.c_ubyte),
            ("lifetime", ctypes.c_ulong),
            ("full_lifetime", ctypes.c_ulong),
        ]

    status = PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None, None
    if status.battery_flag in (128, 255):  # 128 = no battery, 255 = unknown
        return None, None
    percent = None if status.battery_percent == 255 else int(status.battery_percent)
    plugged = None if status.ac_line == 255 else status.ac_line == 1
    return percent, plugged


def _drive_info() -> tuple[DriveInfo, ...]:
    roots: list[str] = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                root = f"{chr(65 + index)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # fixed drive
                    roots.append(root)
    else:
        roots.append("/")
    drives: list[DriveInfo] = []
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append(DriveInfo(
            name=root.rstrip("\\/") or root,
            total_gib=round(usage.total / GIB, 2),
            free_gib=round(usage.free / GIB, 2),
            used_percent=(
                round((usage.total - usage.free) / usage.total * 100, 1)
                if usage.total else 0.0
            ),
        ))
    return tuple(drives)


def _uptime_seconds() -> float | None:
    if os.name != "nt":
        return None
    get_tick_count = ctypes.windll.kernel32.GetTickCount64
    get_tick_count.restype = ctypes.c_ulonglong
    return get_tick_count() / 1000


def _os_name() -> str:
    if os.name == "nt":
        version = platform.win32_ver()
        build = version[1]
        release = platform.release()
        return f"Windows {release}" + (f" (build {build})" if build else "")
    return f"{platform.system()} {platform.release()}".strip()


def take_snapshot(disk_path: str | Path | None = None) -> Snapshot:
    hardware = hardware_info()
    requested_path = Path(disk_path or os.environ.get("SystemDrive", Path.home().anchor) or ".")
    volume_root = requested_path.anchor or str(requested_path)
    system_drive = str(volume_root)
    if len(system_drive) == 2 and system_drive[1] == ":":
        system_drive += "\\"
    drives = _drive_info()
    system_name = system_drive.rstrip("\\/") or system_drive
    system_volume = next((drive for drive in drives if drive.name == system_name), None)
    if system_volume is None:
        disk = shutil.disk_usage(system_drive)
        disk_total_gib = round(disk.total / GIB, 2)
        disk_free_gib = round(disk.free / GIB, 2)
        disk_used_percent = (
            round((disk.total - disk.free) / disk.total * 100, 1) if disk.total else 0.0
        )
    else:
        disk_total_gib = system_volume.total_gib
        disk_free_gib = system_volume.free_gib
        disk_used_percent = system_volume.used_percent
    total_mem, used_mem, available_mem, memory_percent = memory_info()
    battery, plugged = battery_info()
    return Snapshot(
        computer=platform.node() or "Unknown",
        operating_system=_os_name(),
        processor=hardware.cpu_name,
        physical_cores=hardware.physical_cores,
        logical_cpus=hardware.logical_cpus,
        cpu_usage_percent=cpu_usage(),
        memory_installed_gib=hardware.installed_memory_gib,
        memory_total_gib=total_mem,
        memory_used_gib=used_mem,
        memory_available_gib=available_mem,
        memory_used_percent=memory_percent,
        system_drive=system_drive.rstrip("\\/"),
        disk_total_gib=disk_total_gib,
        disk_free_gib=disk_free_gib,
        disk_used_percent=disk_used_percent,
        battery_percent=battery,
        plugged_in=plugged,
        uptime_seconds=_uptime_seconds(),
        captured_at=time.time(),
        drives=drives,
    )


def cpu_self_test(duration: float = 1.0) -> dict[str, object]:
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    validated = hashlib.sha256(b"abc").hexdigest() == expected
    deadline = time.perf_counter() + max(0.1, duration)
    blocks = 0
    payload = b"PC hardware self-test" * 4096
    digest = b""
    while time.perf_counter() < deadline:
        digest = hashlib.sha256(payload + digest).digest()
        blocks += 1
    return {
        "status": "PASS" if validated and blocks > 0 else "FAIL",
        "validated": validated,
        "sha256_blocks": blocks,
        "digest": digest.hex()[:16],
    }


def disk_self_test(size_mb: int = 8) -> dict[str, object]:
    size = max(1, min(size_mb, 64)) * 1024 * 1024
    data = os.urandom(size)
    expected = hashlib.sha256(data).digest()
    path: str | None = None
    started = time.perf_counter()
    actual = b""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as test_file:
            path = test_file.name
            test_file.write(data)
            test_file.flush()
            os.fsync(test_file.fileno())
        written = time.perf_counter()
        actual = hashlib.sha256(Path(path).read_bytes()).digest()
        finished = time.perf_counter()
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
    return {
        "status": "PASS" if actual == expected else "FAIL",
        "size_mb": round(size / 1024**2),
        # These are quick cached-I/O estimates, not full-drive benchmark results.
        "write_mb_s": round(size / 1024**2 / max(written - started, 0.000001), 1),
        "read_mb_s": round(size / 1024**2 / max(finished - written, 0.000001), 1),
    }
