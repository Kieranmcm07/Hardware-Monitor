"""Bounded, background SQLite telemetry history for NEXUS."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # Package destination.
    from .settings import data_directory
except ImportError:  # Standalone staging file.
    from v4_settings import data_directory


@dataclass(frozen=True)
class HistorySample:
    timestamp: float
    cpu_percent: float | None
    memory_percent: float | None
    storage_percent: float | None
    temperature_c: float | None = None
    network_down_bps: float | None = None
    network_up_bps: float | None = None


class HistoryStore:
    """Serialize writes on one daemon thread so the Tk event loop never blocks."""

    def __init__(
        self,
        path: str | Path | None = None,
        retention_days: int = 30,
        queue_size: int = 512,
        autostart: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else data_directory() / "history.sqlite3"
        self.retention_days = min(365, max(1, int(retention_days)))
        self._queue: queue.Queue[HistorySample | object] = queue.Queue(maxsize=max(8, queue_size))
        self._stop = object()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self.dropped_samples = 0
        if autostart:
            self.start()

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._worker, name="nexus-history", daemon=True)
        self._thread.start()

    def add(self, sample: HistorySample) -> bool:
        """Queue a sample. On overload discard one stale sample, never block the GUI."""
        if self._thread is None or not self._thread.is_alive():
            self.start()
        try:
            self._queue.put_nowait(sample)
            return True
        except queue.Full:
            try:
                discarded = self._queue.get_nowait()
                self._queue.task_done()
                if discarded is self._stop:
                    self._queue.put_nowait(discarded)
                    return False
                self.dropped_samples += 1
                self._queue.put_nowait(sample)
                return True
            except queue.Empty:
                return False

    def add_snapshot(
        self,
        snapshot: object,
        *,
        temperature_c: float | None = None,
        network_down_bps: float | None = None,
        network_up_bps: float | None = None,
    ) -> bool:
        return self.add(HistorySample(
            timestamp=float(getattr(snapshot, "captured_at", time.time())),
            cpu_percent=getattr(snapshot, "cpu_usage_percent", None),
            memory_percent=getattr(snapshot, "memory_used_percent", None),
            storage_percent=getattr(snapshot, "disk_used_percent", None),
            temperature_c=temperature_c,
            network_down_bps=network_down_bps,
            network_up_bps=network_up_bps,
        ))

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._queue.unfinished_tasks

    def close(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if not thread or not thread.is_alive():
            return
        deadline = time.monotonic() + max(0.0, timeout)
        self.flush(max(0.0, deadline - time.monotonic()))
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self._queue.put(self._stop, timeout=remaining)
        except queue.Full:
            return
        thread.join(max(0.0, deadline - time.monotonic()))

    def query(self, since: float, until: float | None = None, limit: int = 10_000) -> tuple[HistorySample, ...]:
        if not self.path.exists():
            return ()
        until = time.time() if until is None else float(until)
        limit = min(100_000, max(1, int(limit)))
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=2.0)
            rows = connection.execute(
                "SELECT captured_at,cpu,memory,storage,temperature,down_bps,up_bps "
                "FROM samples WHERE captured_at BETWEEN ? AND ? ORDER BY captured_at LIMIT ?",
                (float(since), until, limit),
            ).fetchall()
        except sqlite3.Error:
            return ()
        finally:
            if connection is not None:
                connection.close()
        return tuple(HistorySample(*row) for row in rows)

    def _worker(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS samples ("
                "captured_at REAL PRIMARY KEY, cpu REAL, memory REAL, storage REAL, "
                "temperature REAL, down_bps REAL, up_bps REAL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS samples_time ON samples(captured_at)")
            self._prune(connection)
            connection.commit()
            pending = 0
            last_prune = time.monotonic()
            while True:
                item = self._queue.get()
                try:
                    if item is self._stop:
                        connection.commit()
                        return
                    assert isinstance(item, HistorySample)
                    connection.execute(
                        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)",
                        (item.timestamp, item.cpu_percent, item.memory_percent,
                         item.storage_percent, item.temperature_c,
                         item.network_down_bps, item.network_up_bps),
                    )
                    pending += 1
                    if pending >= 30 or self._queue.empty():
                        if time.monotonic() - last_prune >= 3_600:
                            self._prune(connection)
                            last_prune = time.monotonic()
                        connection.commit()
                        pending = 0
                finally:
                    self._queue.task_done()
        except Exception as error:  # contained background failure is exposed via .error
            self._error = error
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        finally:
            if connection is not None:
                connection.close()

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = time.time() - self.retention_days * 86_400
        connection.execute("DELETE FROM samples WHERE captured_at < ?", (cutoff,))


def summarize(samples: Iterable[HistorySample]) -> dict[str, float | int | None]:
    values = tuple(samples)
    result: dict[str, float | int | None] = {"samples": len(values)}
    for field, name in (("cpu_percent", "cpu"), ("memory_percent", "memory"),
                        ("storage_percent", "storage"), ("temperature_c", "temperature")):
        series = [float(getattr(sample, field)) for sample in values if getattr(sample, field) is not None]
        result[f"{name}_average"] = round(sum(series) / len(series), 2) if series else None
        result[f"{name}_peak"] = round(max(series), 2) if series else None
    return result
