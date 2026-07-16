from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import re
import shutil
import struct
import sys
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


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _read_text(path: Path, default: str = "") -> str:
    """Read a small kernel metadata file without letting one sensor break a snapshot."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip("\x00\r\n ")
    except (OSError, ValueError):
        return default


def _read_int(path: Path, default: int | None = None) -> int | None:
    try:
        return int(_read_text(path))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DriveInfo:
    name: str
    total_gib: float
    free_gib: float
    used_percent: float


@dataclass(frozen=True)
class NetworkInterfaceInfo:
    luid: int
    index: int
    alias: str
    description: str
    kind: str
    receive_link_bps: int
    transmit_link_bps: int
    received_bytes: int
    sent_bytes: int


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
    monotonic_at: float
    drives: tuple[DriveInfo, ...]
    network_interfaces: tuple[NetworkInterfaceInfo, ...]

    # Compatibility with the original CLI/tests.
    @property
    def memory_total_gb(self) -> float | None:
        return self.memory_total_gib

    @property
    def disk_total_gb(self) -> float:
        return self.disk_total_gib

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _MibIfRow2(ctypes.Structure):
    _fields_ = [
        ("interface_luid", ctypes.c_ulonglong),
        ("interface_index", ctypes.c_ulong),
        ("interface_guid", _Guid),
        ("alias", ctypes.c_wchar * 257),
        ("description", ctypes.c_wchar * 257),
        ("physical_address_length", ctypes.c_ulong),
        ("physical_address", ctypes.c_ubyte * 32),
        ("permanent_physical_address", ctypes.c_ubyte * 32),
        ("mtu", ctypes.c_ulong),
        ("type", ctypes.c_ulong),
        ("tunnel_type", ctypes.c_ulong),
        ("media_type", ctypes.c_ulong),
        ("physical_medium_type", ctypes.c_ulong),
        ("access_type", ctypes.c_ulong),
        ("direction_type", ctypes.c_ulong),
        ("interface_flags", ctypes.c_ubyte),
        ("operational_status", ctypes.c_ulong),
        ("admin_status", ctypes.c_ulong),
        ("media_connect_state", ctypes.c_ulong),
        ("network_guid", _Guid),
        ("connection_type", ctypes.c_ulong),
        ("transmit_link_speed", ctypes.c_ulonglong),
        ("receive_link_speed", ctypes.c_ulonglong),
        ("in_octets", ctypes.c_ulonglong),
        ("in_unicast_packets", ctypes.c_ulonglong),
        ("in_non_unicast_packets", ctypes.c_ulonglong),
        ("in_discards", ctypes.c_ulonglong),
        ("in_errors", ctypes.c_ulonglong),
        ("in_unknown_protocols", ctypes.c_ulonglong),
        ("in_unicast_octets", ctypes.c_ulonglong),
        ("in_multicast_octets", ctypes.c_ulonglong),
        ("in_broadcast_octets", ctypes.c_ulonglong),
        ("out_octets", ctypes.c_ulonglong),
        ("out_unicast_packets", ctypes.c_ulonglong),
        ("out_non_unicast_packets", ctypes.c_ulonglong),
        ("out_discards", ctypes.c_ulonglong),
        ("out_errors", ctypes.c_ulonglong),
        ("out_unicast_octets", ctypes.c_ulonglong),
        ("out_multicast_octets", ctypes.c_ulonglong),
        ("out_broadcast_octets", ctypes.c_ulonglong),
        ("out_queue_length", ctypes.c_ulonglong),
    ]


class _MibIfTable2(ctypes.Structure):
    _fields_ = [("num_entries", ctypes.c_ulong), ("table", _MibIfRow2 * 1)]


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


def _parse_linux_cpuinfo(
    text: str,
) -> tuple[str, int | None, int, int | None]:
    """Parse /proc/cpuinfo into name, physical cores, threads and peak MHz."""
    records: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        record: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            record[key.strip().lower()] = value.strip()
        if record:
            records.append(record)

    names: list[str] = []
    for key in ("model name", "hardware", "cpu model", "processor"):
        for record in records:
            candidate = _clean(record.get(key), "")
            if candidate and not candidate.isdecimal():
                names.append(candidate)
        if names:
            break
    cpu_name = names[0] if names else "Unknown processor"

    processor_records = [record for record in records if "processor" in record]
    logical = len(processor_records) or len(records) or 1
    core_pairs: set[tuple[str, str]] = set()
    for record in processor_records:
        physical_id = record.get("physical id")
        core_id = record.get("core id")
        if physical_id is not None and core_id is not None:
            core_pairs.add((physical_id, core_id))
    physical: int | None = len(core_pairs) or None
    if physical is None:
        socket_cores: dict[str, int] = {}
        for record in processor_records:
            try:
                cores = int(record.get("cpu cores", ""))
            except ValueError:
                continue
            socket = record.get("physical id", "0")
            socket_cores[socket] = max(socket_cores.get(socket, 0), cores)
        if socket_cores:
            physical = sum(socket_cores.values())

    frequencies: list[float] = []
    for record in records:
        try:
            frequencies.append(float(record.get("cpu mhz", "")))
        except ValueError:
            pass
    max_mhz = round(max(frequencies)) if frequencies else None
    return cpu_name, physical, logical, max_mhz


def _linux_physical_core_count(sys_root: Path = Path("/sys")) -> int | None:
    """Read Linux CPU topology without guessing that every thread is a core."""
    core_pairs: set[tuple[str, str]] = set()
    cpu_root = sys_root / "devices" / "system" / "cpu"
    for cpu in cpu_root.glob("cpu[0-9]*"):
        topology = cpu / "topology"
        core_id = _read_text(topology / "core_id")
        if not core_id:
            continue
        package_id = _read_text(topology / "physical_package_id") or "0"
        core_pairs.add((package_id, core_id))
    return len(core_pairs) or None


def _linux_cpu_max_mhz(sys_root: Path, fallback: int | None) -> int | None:
    frequencies: list[int] = []
    cpu_root = sys_root / "devices" / "system" / "cpu"
    for cpu in cpu_root.glob("cpu[0-9]*"):
        for filename in ("cpuinfo_max_freq", "scaling_max_freq"):
            khz = _read_int(cpu / "cpufreq" / filename)
            if khz is not None and khz > 0:
                frequencies.append(khz)
                break
    return round(max(frequencies) / 1000) if frequencies else fallback


def _parse_equals_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value.replace(r"\n", " ").replace(r'\"', '"')
    return values


def _linux_gpu_names(sys_root: Path, proc_root: Path) -> tuple[str, ...]:
    names: list[str] = []
    for information in (proc_root / "driver" / "nvidia" / "gpus").glob("*/information"):
        for line in _read_text(information).splitlines():
            if line.lower().startswith("model:"):
                model = _clean(line.split(":", 1)[1], "")
                if model and model not in names:
                    names.append(model)
                break
    has_specific_nvidia_name = bool(names)

    vendor_names = {
        "0x1002": "AMD",
        "0x10de": "NVIDIA",
        "0x8086": "Intel",
    }
    for card in (sys_root / "class" / "drm").glob("card[0-9]*"):
        if not card.name.removeprefix("card").isdigit():
            continue
        device = card / "device"
        uevent = _parse_equals_file(_read_text(device / "uevent"))
        vendor_id = _read_text(device / "vendor").lower()
        device_id = _read_text(device / "device").lower().removeprefix("0x")
        driver = uevent.get("DRIVER", "")
        pci_id = uevent.get("PCI_ID", "")
        if not pci_id and vendor_id:
            pci_id = f"{vendor_id.removeprefix('0x')}:{device_id}".rstrip(":").upper()
        vendor = vendor_names.get(vendor_id, "GPU")
        if vendor == "NVIDIA" and has_specific_nvidia_name:
            # /proc supplied the product model already; avoid a second generic
            # card for the same GPU from DRM sysfs.
            continue
        detail = f" {pci_id}" if pci_id else ""
        driver_detail = f" ({driver} driver)" if driver else ""
        description = f"{vendor} GPU{detail}{driver_detail}"
        if description not in names:
            names.append(description)
    return tuple(names)


def _linux_installed_memory_gib(proc_root: Path) -> float | None:
    values = _parse_linux_meminfo(_read_text(proc_root / "meminfo"))
    total = values.get("MemTotal")
    return round(total / GIB, 2) if total else None


def _linux_hardware_info(
    proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")
) -> HardwareInfo:
    cpu_name, physical, logical, proc_mhz = _parse_linux_cpuinfo(
        _read_text(proc_root / "cpuinfo")
    )
    physical = physical or _linux_physical_core_count(sys_root)
    board_vendor = _clean(_read_text(sys_root / "class" / "dmi" / "id" / "board_vendor"), "")
    board_name = _clean(_read_text(sys_root / "class" / "dmi" / "id" / "board_name"), "")
    if not board_vendor and not board_name:
        board_name = _clean(_read_text(proc_root / "device-tree" / "model"), "")
    motherboard = " ".join(value for value in (board_vendor, board_name) if value)
    bios = _clean(_read_text(sys_root / "class" / "dmi" / "id" / "bios_version"))
    return HardwareInfo(
        cpu_name=cpu_name,
        physical_cores=physical,
        logical_cpus=logical or os.cpu_count() or 1,
        cpu_max_mhz=_linux_cpu_max_mhz(sys_root, proc_mhz),
        gpu_names=_linux_gpu_names(sys_root, proc_root),
        motherboard=motherboard or "Unavailable",
        bios_version=bios,
        installed_memory_gib=_linux_installed_memory_gib(proc_root),
        architecture=platform.machine() or "Unknown",
    )


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
    if _is_linux():
        return _linux_hardware_info()

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


def _parse_linux_cpu_times(text: str) -> tuple[int, int] | None:
    """Return idle and total jiffies from the aggregate /proc/stat CPU row."""
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            values = [int(value) for value in fields[1:9]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        values.extend([0] * (8 - len(values)))
        # guest and guest_nice (later fields) are already included in user/nice.
        idle = values[3] + values[4]
        return idle, sum(values)
    return None


def _linux_cpu_times(proc_root: Path = Path("/proc")) -> tuple[int, int] | None:
    return _parse_linux_cpu_times(_read_text(proc_root / "stat"))


def cpu_usage(sample_seconds: float = 0.25) -> float | None:
    sample = _linux_cpu_times if _is_linux() else _windows_cpu_times
    first = sample()
    if first is None:
        return None
    time.sleep(max(0.05, sample_seconds))
    second = sample()
    if second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100 * (1 - idle_delta / total_delta))), 1)


def _parse_linux_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo values and normalize them to bytes."""
    values: dict[str, int] = {}
    factors = {"kb": 1024, "mb": 1024**2, "gb": GIB}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        fields = raw_value.split()
        if not fields:
            continue
        try:
            amount = int(fields[0])
        except ValueError:
            continue
        factor = factors.get(fields[1].lower(), 1) if len(fields) > 1 else 1
        values[key.strip()] = amount * factor
    return values


def _linux_memory_info(
    proc_root: Path = Path("/proc"),
) -> tuple[float | None, float | None, float | None, float | None]:
    values = _parse_linux_meminfo(_read_text(proc_root / "meminfo"))
    total_bytes = values.get("MemTotal")
    if not total_bytes:
        return None, None, None, None
    available_bytes = values.get("MemAvailable")
    if available_bytes is None:
        available_bytes = (
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
            + values.get("SReclaimable", 0)
            - values.get("Shmem", 0)
        )
    available_bytes = max(0, min(total_bytes, available_bytes))
    used_bytes = total_bytes - available_bytes
    return (
        round(total_bytes / GIB, 2),
        round(used_bytes / GIB, 2),
        round(available_bytes / GIB, 2),
        round(used_bytes / total_bytes * 100, 1),
    )


def memory_info() -> tuple[float | None, float | None, float | None, float | None]:
    if _is_linux():
        return _linux_memory_info()
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


def _linux_battery_info(sys_root: Path = Path("/sys")) -> tuple[int | None, bool | None]:
    supply_root = sys_root / "class" / "power_supply"
    try:
        supplies = tuple(supply_root.iterdir())
    except OSError:
        return None, None

    percentages: list[float] = []
    energy_totals = [0, 0]
    charge_totals = [0, 0]
    battery_count = 0
    energy_count = 0
    charge_count = 0
    battery_statuses: list[str] = []
    ac_states: list[bool] = []
    for supply in supplies:
        supply_type = _read_text(supply / "type").lower()
        is_battery = supply_type == "battery" or supply.name.upper().startswith("BAT")
        if is_battery:
            battery_count += 1
            percent = _read_int(supply / "capacity")
            derived_percent: float | None = None
            for prefix, totals in (("energy", energy_totals), ("charge", charge_totals)):
                now = _read_int(supply / f"{prefix}_now")
                full = _read_int(supply / f"{prefix}_full")
                if now is None or full is None or full <= 0:
                    continue
                totals[0] += max(0, min(now, full))
                totals[1] += full
                if prefix == "energy":
                    energy_count += 1
                else:
                    charge_count += 1
                derived_percent = max(0.0, min(100.0, now / full * 100))
                break
            if percent is not None:
                percentages.append(max(0, min(100, percent)))
            elif derived_percent is not None:
                percentages.append(derived_percent)
            status = _read_text(supply / "status").lower()
            if status:
                battery_statuses.append(status)
            continue
        online = _read_int(supply / "online")
        if online is not None:
            ac_states.append(online == 1)

    if battery_count and energy_count == battery_count and energy_totals[1]:
        percent = round(energy_totals[0] / energy_totals[1] * 100)
    elif battery_count and charge_count == battery_count and charge_totals[1]:
        percent = round(charge_totals[0] / charge_totals[1] * 100)
    else:
        percent = round(sum(percentages) / len(percentages)) if percentages else None
    percent = max(0, min(100, percent)) if percent is not None else None
    if ac_states:
        plugged: bool | None = any(ac_states)
    elif any(status in {"charging", "full", "not charging"} for status in battery_statuses):
        plugged = True
    elif any(status == "discharging" for status in battery_statuses):
        plugged = False
    else:
        plugged = None
    return percent, plugged


def battery_info() -> tuple[int | None, bool | None]:
    if _is_linux():
        return _linux_battery_info()
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


def _network_interface_from_row(row: _MibIfRow2) -> NetworkInterfaceInfo | None:
    """Convert one native row, excluding pseudo and disconnected interfaces."""
    is_hardware = bool(row.interface_flags & 0x01)
    is_filter = bool(row.interface_flags & 0x02)
    is_endpoint = bool(row.interface_flags & 0x80)
    is_connected = row.operational_status == 1 and row.media_connect_state == 1
    if not is_hardware or is_filter or is_endpoint or not is_connected:
        return None
    if row.type == 24:  # software loopback
        return None
    kind = {
        6: "Ethernet",
        71: "Wi-Fi",
        243: "Mobile broadband",
        244: "Mobile broadband",
    }.get(int(row.type), "Network")
    return NetworkInterfaceInfo(
        luid=int(row.interface_luid),
        index=int(row.interface_index),
        alias=row.alias.strip("\x00 ") or f"Interface {row.interface_index}",
        description=row.description.strip("\x00 ") or "Windows network adapter",
        kind=kind,
        receive_link_bps=int(row.receive_link_speed),
        transmit_link_bps=int(row.transmit_link_speed),
        received_bytes=int(row.in_octets),
        sent_bytes=int(row.out_octets),
    )


def _network_interfaces_from_table(table_address: int) -> tuple[NetworkInterfaceInfo, ...]:
    """Parse a GetIfTable2 allocation while preserving its 64-bit counters."""
    interfaces: list[NetworkInterfaceInfo] = []
    count = ctypes.c_ulong.from_address(table_address).value
    row_offset = _MibIfTable2.table.offset
    row_size = ctypes.sizeof(_MibIfRow2)
    for position in range(count):
        row = _MibIfRow2.from_address(table_address + row_offset + position * row_size)
        interface = _network_interface_from_row(row)
        if interface is not None:
            interfaces.append(interface)
    return tuple(sorted(interfaces, key=lambda interface: (interface.kind, interface.alias)))


def _linux_network_interface(interface: Path) -> NetworkInterfaceInfo | None:
    """Build one physical, connected adapter from Linux sysfs counters."""
    if interface.name == "lo":
        return None
    device = interface / "device"
    wireless = interface / "wireless"
    if not device.exists() and not wireless.exists():
        return None
    operational_state = _read_text(interface / "operstate").lower()
    carrier = _read_int(interface / "carrier")
    if operational_state not in {"up", "unknown"} or carrier == 0:
        return None
    index = _read_int(interface / "ifindex")
    if index is None or index <= 0:
        return None

    interface_type = _read_int(interface / "type")
    if wireless.exists() or interface.name.startswith(("wl", "wifi")):
        kind = "Wi-Fi"
    elif interface.name.startswith(("wwan", "wwp")):
        kind = "Mobile broadband"
    elif interface_type == 1:
        kind = "Ethernet"
    else:
        kind = "Network"

    uevent = _parse_equals_file(_read_text(device / "uevent"))
    driver = uevent.get("DRIVER", "")
    description = f"{kind} adapter"
    if driver:
        description += f" ({driver} driver)"
    speed_mbps = _read_int(interface / "speed", 0) or 0
    speed_bps = max(0, speed_mbps) * 1_000_000
    received = _read_int(interface / "statistics" / "rx_bytes", 0) or 0
    sent = _read_int(interface / "statistics" / "tx_bytes", 0) or 0
    return NetworkInterfaceInfo(
        luid=index,
        index=index,
        alias=interface.name,
        description=description,
        kind=kind,
        receive_link_bps=speed_bps,
        transmit_link_bps=speed_bps,
        received_bytes=max(0, received),
        sent_bytes=max(0, sent),
    )


def _linux_network_interfaces(
    sys_root: Path = Path("/sys"),
) -> tuple[NetworkInterfaceInfo, ...]:
    interfaces: list[NetworkInterfaceInfo] = []
    try:
        paths = tuple((sys_root / "class" / "net").iterdir())
    except OSError:
        return ()
    for path in paths:
        interface = _linux_network_interface(path)
        if interface is not None:
            interfaces.append(interface)
    return tuple(sorted(interfaces, key=lambda interface: (interface.kind, interface.alias)))


def network_interfaces() -> tuple[NetworkInterfaceInfo, ...]:
    """Return connected physical adapters with native 64-bit byte counters."""
    if _is_linux():
        return _linux_network_interfaces()
    if os.name != "nt":
        return ()
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    iphlpapi.GetIfTable2.restype = ctypes.c_ulong
    iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
    table_pointer = ctypes.c_void_p()
    if iphlpapi.GetIfTable2(ctypes.byref(table_pointer)) != 0 or not table_pointer.value:
        return ()
    try:
        return _network_interfaces_from_table(table_pointer.value)
    finally:
        iphlpapi.FreeMibTable(table_pointer)


_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_LINUX_LOCAL_FILESYSTEMS = {
    "bcachefs",
    "btrfs",
    "drvfs",
    "exfat",
    "ext2",
    "ext3",
    "ext4",
    "f2fs",
    "fuseblk",
    "hfs",
    "hfsplus",
    "jfs",
    "msdos",
    "ntfs",
    "ntfs3",
    "reiserfs",
    "udf",
    "vfat",
    "xfs",
    "zfs",
}


def _decode_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _parse_linux_mountinfo(text: str) -> tuple[str, ...]:
    """Return local-volume mount points from /proc/self/mountinfo."""
    mounts: list[str] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _decode_mount_field(fields[4])
            filesystem = fields[separator + 1].lower()
            source = _decode_mount_field(fields[separator + 2])
        except (IndexError, ValueError):
            continue
        is_local = filesystem in _LINUX_LOCAL_FILESYSTEMS or source.startswith("/dev/")
        if is_local and mount_point not in mounts:
            mounts.append(mount_point)
    return tuple(mounts)


def _linux_drive_roots(proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    roots = ["/"]
    for root in _parse_linux_mountinfo(_read_text(proc_root / "self" / "mountinfo")):
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _disk_used_percent(usage: shutil._ntuple_diskusage, linux: bool = False) -> float:
    if linux:
        # Match df(1): reserved filesystem blocks are neither used nor available.
        denominator = usage.used + usage.free
        return round(usage.used / denominator * 100, 1) if denominator else 0.0
    return round((usage.total - usage.free) / usage.total * 100, 1) if usage.total else 0.0


def _drive_info() -> tuple[DriveInfo, ...]:
    roots: list[str] = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                root = f"{chr(65 + index)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # fixed drive
                    roots.append(root)
    elif _is_linux():
        roots.extend(_linux_drive_roots())
    else:
        roots.append("/")
    drives: list[DriveInfo] = []
    seen_volumes: set[tuple[int, int]] = set()
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
            if _is_linux():
                volume_identity = (os.stat(root).st_dev, usage.total)
                if volume_identity in seen_volumes:
                    continue
                seen_volumes.add(volume_identity)
        except OSError:
            continue
        drives.append(DriveInfo(
            name=root.rstrip("\\/") or root,
            total_gib=round(usage.total / GIB, 2),
            free_gib=round(usage.free / GIB, 2),
            used_percent=_disk_used_percent(usage, linux=_is_linux()),
        ))
    return tuple(drives)


def _parse_linux_uptime(text: str) -> float | None:
    try:
        value = float(text.split()[0])
    except (IndexError, ValueError):
        return None
    return value if value >= 0 else None


def _linux_uptime_seconds(proc_root: Path = Path("/proc")) -> float | None:
    return _parse_linux_uptime(_read_text(proc_root / "uptime"))


def _uptime_seconds() -> float | None:
    if _is_linux():
        return _linux_uptime_seconds()
    if os.name != "nt":
        return None
    get_tick_count = ctypes.windll.kernel32.GetTickCount64
    get_tick_count.restype = ctypes.c_ulonglong
    return get_tick_count() / 1000


def _parse_os_release(text: str) -> str | None:
    values = _parse_equals_file(text)
    pretty_name = _clean(values.get("PRETTY_NAME"), "")
    if pretty_name:
        return pretty_name
    name = _clean(values.get("NAME"), "")
    version = _clean(values.get("VERSION"), "")
    combined = " ".join(value for value in (name, version) if value)
    return combined or None


def _linux_os_name(etc_root: Path = Path("/etc")) -> str:
    release_name = _parse_os_release(_read_text(etc_root / "os-release"))
    if release_name:
        return release_name
    return f"Linux {platform.release()}".strip()


def _os_name() -> str:
    if os.name == "nt":
        version = platform.win32_ver()
        build = version[1]
        release = platform.release()
        return f"Windows {release}" + (f" (build {build})" if build else "")
    if _is_linux():
        return _linux_os_name()
    return f"{platform.system()} {platform.release()}".strip()


def take_snapshot(disk_path: str | Path | None = None) -> Snapshot:
    hardware = hardware_info()
    if disk_path is not None:
        requested_path = Path(disk_path)
    elif _is_linux():
        # Do not inherit a Windows SystemDrive variable under WSL or Linux shells.
        requested_path = Path("/")
    elif os.name == "nt":
        requested_path = Path(os.environ.get("SystemDrive", Path.home().anchor) or ".")
    else:
        requested_path = Path("/")
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
        disk_used_percent = _disk_used_percent(disk, linux=_is_linux())
    else:
        disk_total_gib = system_volume.total_gib
        disk_free_gib = system_volume.free_gib
        disk_used_percent = system_volume.used_percent
    total_mem, used_mem, available_mem, memory_percent = memory_info()
    battery, plugged = battery_info()
    interfaces = network_interfaces()
    captured_at = time.time()
    monotonic_at = time.monotonic()
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
        system_drive=system_name,
        disk_total_gib=disk_total_gib,
        disk_free_gib=disk_free_gib,
        disk_used_percent=disk_used_percent,
        battery_percent=battery,
        plugged_in=plugged,
        uptime_seconds=_uptime_seconds(),
        captured_at=captured_at,
        monotonic_at=monotonic_at,
        drives=drives,
        network_interfaces=interfaces,
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
