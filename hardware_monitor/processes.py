"""Read-only native process explorer for Windows and Linux (no extra packages)."""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawProcess:
    pid: int
    name: str
    cpu_seconds: float | None
    rss_bytes: int | None
    start_id: int | float | None
    executable: str = ""
    threads: int | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float | None
    memory_mib: float | None
    memory_percent: float | None
    executable: str
    threads: int | None


@dataclass(frozen=True)
class ProcessSnapshot:
    captured_at: float
    processes: tuple[ProcessInfo, ...]
    inaccessible_count: int = 0


def _parse_linux_stat(text: str) -> tuple[str, int, int, int, int]:
    """Return name, utime, stime, threads and start ticks from /proc/PID/stat."""
    left = text.find("(")
    right = text.rfind(")")
    if left < 1 or right <= left:
        raise ValueError("invalid proc stat")
    name = text[left + 1:right]
    fields = text[right + 2:].split()
    if len(fields) < 20:
        raise ValueError("incomplete proc stat")
    return name, int(fields[11]), int(fields[12]), int(fields[17]), int(fields[19])


def _linux_total_memory(proc_root: Path) -> int | None:
    try:
        for line in (proc_root / "meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _linux_raw_processes(proc_root: Path = Path("/proc")) -> tuple[tuple[RawProcess, ...], int, int | None]:
    clock_ticks = os.sysconf("SC_CLK_TCK")
    page_size = os.sysconf("SC_PAGE_SIZE")
    output: list[RawProcess] = []
    inaccessible = 0
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return (), 0, None
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            name, user_ticks, system_ticks, threads, start_ticks = _parse_linux_stat(
                (entry / "stat").read_text(encoding="utf-8", errors="replace")
            )
            rss_pages = int((entry / "statm").read_text(encoding="ascii").split()[1])
            try:
                executable = os.readlink(entry / "exe")
            except OSError:
                executable = ""
            output.append(RawProcess(
                pid=int(entry.name), name=name,
                cpu_seconds=(user_ticks + system_ticks) / clock_ticks,
                rss_bytes=max(0, rss_pages * page_size), start_id=start_ticks,
                executable=executable, threads=max(0, threads),
            ))
        except (OSError, ValueError, IndexError):
            inaccessible += 1
    return tuple(output), inaccessible, _linux_total_memory(proc_root)


def _filetime_value(value: object) -> int:
    return (int(getattr(value, "dwHighDateTime")) << 32) | int(getattr(value, "dwLowDateTime"))


def _windows_raw_processes() -> tuple[tuple[RawProcess, ...], int, int | None]:
    if os.name != "nt":
        return (), 0, None
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return (), 0, _windows_total_memory()
    output: list[RawProcess] = []
    inaccessible = 0
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            pid = int(entry.th32ProcessID)
            raw = _windows_process_detail(kernel32, psapi, ProcessMemoryCounters, pid)
            if raw is None:
                inaccessible += 1
                output.append(RawProcess(pid, entry.szExeFile, None, None, None,
                                         threads=int(entry.cntThreads)))
            else:
                output.append(RawProcess(pid, entry.szExeFile, *raw,
                                         threads=int(entry.cntThreads)))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(output), inaccessible, _windows_total_memory()


def _windows_process_detail(kernel32: object, psapi: object, counters_type: type,
                            pid: int) -> tuple[float, int, int, str] | None:
    from ctypes import wintypes
    process = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not process:
        return None
    try:
        created = wintypes.FILETIME(); exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME(); user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        counters = counters_type(); counters.cb = ctypes.sizeof(counters)
        rss = int(counters.WorkingSetSize) if psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ) else 0
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        executable = buffer.value if kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(capacity)
        ) else ""
        seconds = (_filetime_value(kernel) + _filetime_value(user)) / 10_000_000
        return seconds, rss, _filetime_value(created), executable
    finally:
        kernel32.CloseHandle(process)


def _windows_total_memory() -> int | None:
    if os.name != "nt":
        return None
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]
    status = Status(); status.length = ctypes.sizeof(status)
    return int(status.total_phys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None


class ProcessTracker:
    """Convert cumulative native CPU times into sortable interval percentages."""

    def __init__(self, logical_cpus: int | None = None) -> None:
        self.logical_cpus = max(1, logical_cpus or os.cpu_count() or 1)
        self._previous: dict[int, tuple[int | float | None, float | None]] = {}
        self._previous_at: float | None = None

    def update(
        self, raw_processes: tuple[RawProcess, ...] | list[RawProcess],
        captured_at: float | None = None, total_memory: int | None = None,
        inaccessible_count: int = 0,
    ) -> ProcessSnapshot:
        now = time.monotonic() if captured_at is None else float(captured_at)
        elapsed = None if self._previous_at is None else max(0.000001, now - self._previous_at)
        processes: list[ProcessInfo] = []
        current: dict[int, tuple[int | float | None, float | None]] = {}
        for raw in raw_processes:
            current[raw.pid] = (raw.start_id, raw.cpu_seconds)
            cpu_percent: float | None = None
            previous = self._previous.get(raw.pid)
            if elapsed is not None and previous and previous[0] == raw.start_id:
                if raw.cpu_seconds is not None and previous[1] is not None:
                    delta = max(0.0, raw.cpu_seconds - previous[1])
                    cpu_percent = round(min(100.0, delta / elapsed * 100 / self.logical_cpus), 1)
            memory_mib = round(raw.rss_bytes / 1024**2, 1) if raw.rss_bytes is not None else None
            memory_percent = (
                round(raw.rss_bytes / total_memory * 100, 2)
                if raw.rss_bytes is not None and total_memory else None
            )
            processes.append(ProcessInfo(raw.pid, raw.name, cpu_percent, memory_mib,
                                         memory_percent, raw.executable, raw.threads))
        self._previous = current
        self._previous_at = now
        processes.sort(key=lambda item: (-(item.cpu_percent or 0), -(item.memory_mib or 0), item.name.lower()))
        return ProcessSnapshot(now, tuple(processes), inaccessible_count)

    def sample(self) -> ProcessSnapshot:
        if os.name == "nt":
            raw, inaccessible, total = _windows_raw_processes()
        elif os.name == "posix":
            raw, inaccessible, total = _linux_raw_processes()
        else:
            raw, inaccessible, total = (), 0, None
        return self.update(raw, total_memory=total, inaccessible_count=inaccessible)


def take_process_snapshot(tracker: ProcessTracker | None = None) -> ProcessSnapshot:
    return (tracker or ProcessTracker()).sample()
