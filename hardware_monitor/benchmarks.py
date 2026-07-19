"""Short, cancellable safety-focused CPU, memory and temporary-file benchmarks."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class BenchmarkResult:
    name: str
    status: str
    score: float | None
    unit: str
    duration_seconds: float
    detail: str = ""


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def cpu_benchmark(
    duration: float = 1.5, cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> BenchmarkResult:
    duration = min(10.0, max(0.1, float(duration)))
    validation = hashlib.sha256(b"abc").hexdigest()
    if validation != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad":
        return BenchmarkResult("CPU", "failed", None, "MiB/s", 0, "SHA-256 validation failed")
    started = time.perf_counter(); deadline = started + duration
    payload = b"NEXUS hardware benchmark" * 4096
    digest = b""; processed = 0
    while time.perf_counter() < deadline:
        if _cancelled(cancel):
            return BenchmarkResult("CPU", "cancelled", None, "MiB/s",
                                   time.perf_counter() - started)
        digest = hashlib.sha256(payload + digest).digest()
        processed += len(payload)
        if progress:
            progress("CPU", min(1.0, (time.perf_counter() - started) / duration))
    elapsed = time.perf_counter() - started
    return BenchmarkResult("CPU", "passed", round(processed / 1024**2 / elapsed, 1),
                           "MiB/s", round(elapsed, 3), digest.hex()[:16])


def memory_benchmark(
    size_mib: int = 64, rounds: int = 8, cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> BenchmarkResult:
    size = min(256, max(1, int(size_mib))) * 1024**2
    rounds = min(100, max(1, int(rounds)))
    try:
        pattern = bytes(range(256))
        source = bytearray((pattern * (size // len(pattern) + 1))[:size])
        target = bytearray(size)
    except MemoryError:
        return BenchmarkResult("Memory", "failed", None, "GiB/s", 0,
                               "Not enough free memory for the test buffer")
    started = time.perf_counter()
    for index in range(rounds):
        if _cancelled(cancel):
            return BenchmarkResult("Memory", "cancelled", None, "GiB/s",
                                   time.perf_counter() - started)
        target[:] = source
        source, target = target, source
        if progress:
            progress("Memory", (index + 1) / rounds)
    elapsed = time.perf_counter() - started
    gib_per_second = size * rounds / 1024**3 / max(elapsed, 0.000001)
    return BenchmarkResult("Memory", "passed", round(gib_per_second, 2), "GiB/s",
                           round(elapsed, 3), f"{size // 1024**2} MiB × {rounds} copies")


def disk_benchmark(
    size_mib: int = 64, directory: str | Path | None = None,
    cancel: threading.Event | None = None, progress: ProgressCallback | None = None,
) -> tuple[BenchmarkResult, BenchmarkResult]:
    size = min(512, max(1, int(size_mib))) * 1024**2
    chunk = b"NEXUS" * (1024**2 // 5)
    chunk = chunk[:1024**2]
    path: Path | None = None
    written = 0
    write_started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=directory, prefix="nexus-benchmark-") as handle:
            path = Path(handle.name)
            while written < size:
                if _cancelled(cancel):
                    elapsed = time.perf_counter() - write_started
                    cancelled = BenchmarkResult("Disk write", "cancelled", None, "MiB/s", elapsed)
                    return cancelled, BenchmarkResult("Disk read", "cancelled", None, "MiB/s", 0)
                data = chunk[:min(len(chunk), size - written)]
                handle.write(data); written += len(data)
                if progress:
                    progress("Disk write", written / size)
            handle.flush(); os.fsync(handle.fileno())
        write_elapsed = time.perf_counter() - write_started
        read_started = time.perf_counter(); read_bytes = 0
        with path.open("rb") as handle:
            while True:
                if _cancelled(cancel):
                    cancelled = BenchmarkResult("Disk read", "cancelled", None, "MiB/s",
                                                time.perf_counter() - read_started)
                    write = BenchmarkResult("Disk write", "passed", round(size / 1024**2 / write_elapsed, 1),
                                            "MiB/s", round(write_elapsed, 3))
                    return write, cancelled
                data = handle.read(1024**2)
                if not data:
                    break
                read_bytes += len(data)
                if progress:
                    progress("Disk read", read_bytes / size)
        read_elapsed = time.perf_counter() - read_started
        return (
            BenchmarkResult("Disk write", "passed", round(size / 1024**2 / write_elapsed, 1),
                            "MiB/s", round(write_elapsed, 3), "Temporary file; not a full-drive test"),
            BenchmarkResult("Disk read", "passed", round(read_bytes / 1024**2 / max(read_elapsed, .000001), 1),
                            "MiB/s", round(read_elapsed, 3), "Temporary file; OS caching may affect this result"),
        )
    except OSError as error:
        failed = BenchmarkResult("Disk write", "failed", None, "MiB/s",
                                 time.perf_counter() - write_started, str(error))
        return failed, BenchmarkResult("Disk read", "failed", None, "MiB/s", 0, str(error))
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


class BenchmarkRunner:
    """Run the complete suite on one worker and support cooperative cancellation."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(
        self, complete: Callable[[tuple[BenchmarkResult, ...]], None],
        progress: ProgressCallback | None = None,
    ) -> bool:
        if self.running:
            return False
        self.cancel_event.clear()
        def work() -> None:
            results = [cpu_benchmark(cancel=self.cancel_event, progress=progress)]
            if not self.cancel_event.is_set():
                results.append(memory_benchmark(cancel=self.cancel_event, progress=progress))
            if not self.cancel_event.is_set():
                results.extend(disk_benchmark(cancel=self.cancel_event, progress=progress))
            complete(tuple(results))
        self.thread = threading.Thread(target=work, name="nexus-benchmark", daemon=True)
        self.thread.start()
        return True

    def cancel(self) -> None:
        self.cancel_event.set()
