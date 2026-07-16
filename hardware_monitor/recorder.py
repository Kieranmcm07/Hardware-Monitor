from __future__ import annotations

import csv
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class TelemetrySample:
    timestamp: float
    cpu_percent: float | None
    memory_percent: float | None
    memory_used_gib: float | None
    system_drive: str
    storage_used_percent: float
    storage_free_gib: float
    fixed_drives: str
    download_bps: float | None
    upload_bps: float | None
    network_received_session_bytes: int | None
    network_sent_session_bytes: int | None
    alert: str


class SessionRecorder:
    """Collects monitor snapshots for session statistics and CSV export."""

    # At a one-second refresh this retains the latest 24 hours for CSV export.
    # Aggregate session statistics remain available after older rows roll off.
    DEFAULT_MAX_SAMPLES = 86_400
    fieldnames = tuple(TelemetrySample.__dataclass_fields__)

    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be at least 1")
        self._samples: deque[TelemetrySample] = deque(maxlen=max_samples)
        self.active = True
        self._reset_summary()

    @property
    def samples(self) -> tuple[TelemetrySample, ...]:
        return tuple(self._samples)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def pause(self, monotonic_at: float | None = None) -> None:
        if not self.active:
            return
        paused_at = time.monotonic() if monotonic_at is None else float(monotonic_at)
        if self._last_monotonic is not None:
            self._active_duration += max(0.0, paused_at - self._last_monotonic)
        self.active = False
        self._last_monotonic = None
        self._accept_after_monotonic = None

    def resume(self, monotonic_at: float | None = None) -> None:
        if self.active:
            return
        self.active = True
        # Start timing immediately while still excluding the paused interval.
        self._last_monotonic = (
            time.monotonic() if monotonic_at is None else float(monotonic_at)
        )
        self._accept_after_monotonic = self._last_monotonic

    def reset(self) -> None:
        self._samples.clear()
        self.active = True
        self._reset_summary()

    def _reset_summary(self) -> None:
        self._total_samples = 0
        self._active_duration = 0.0
        self._last_monotonic: float | None = None
        self._accept_after_monotonic: float | None = None
        self._cpu_total = 0.0
        self._cpu_count = 0
        self._cpu_peak: float | None = None
        self._memory_total = 0.0
        self._memory_count = 0
        self._memory_peak: float | None = None
        self._alert_samples = 0

    def capture(self, snapshot, network=None) -> TelemetrySample | None:
        if not self.active:
            return None
        monotonic_at = float(getattr(snapshot, "monotonic_at", snapshot.captured_at))
        if (
            self._accept_after_monotonic is not None
            and monotonic_at < self._accept_after_monotonic
        ):
            # take_snapshot timestamps before its CPU sample finishes, so a
            # pre-resume frame can arrive just after the button was clicked.
            return None
        self._accept_after_monotonic = None
        alerts: list[str] = []
        if snapshot.cpu_usage_percent is not None and snapshot.cpu_usage_percent >= 85:
            alerts.append("CPU >= 85%")
        if snapshot.memory_used_percent is not None and snapshot.memory_used_percent >= 85:
            alerts.append("RAM >= 85%")
        drives = tuple(getattr(snapshot, "drives", ()))
        critical_drives = [drive for drive in drives if drive.used_percent >= 90]
        if critical_drives:
            alerts.extend(f"{drive.name} storage >= 90%" for drive in critical_drives)
        elif not drives and snapshot.disk_used_percent >= 90:
            alerts.append(f"{snapshot.system_drive} storage >= 90%")
        drive_summary = "; ".join(
            f"{drive.name} {drive.used_percent:.1f}% used, {drive.free_gib:.1f} GiB free"
            for drive in drives
        )
        download_bps = getattr(network, "download_bps", None)
        upload_bps = getattr(network, "upload_bps", None)
        received_bytes = getattr(network, "session_received_bytes", None)
        sent_bytes = getattr(network, "session_sent_bytes", None)
        sample = TelemetrySample(
            timestamp=float(snapshot.captured_at),
            cpu_percent=snapshot.cpu_usage_percent,
            memory_percent=snapshot.memory_used_percent,
            memory_used_gib=snapshot.memory_used_gib,
            system_drive=snapshot.system_drive,
            storage_used_percent=snapshot.disk_used_percent,
            storage_free_gib=snapshot.disk_free_gib,
            fixed_drives=drive_summary,
            download_bps=float(download_bps) if download_bps is not None else None,
            upload_bps=float(upload_bps) if upload_bps is not None else None,
            network_received_session_bytes=(
                int(received_bytes) if received_bytes is not None else None
            ),
            network_sent_session_bytes=int(sent_bytes) if sent_bytes is not None else None,
            alert="; ".join(alerts),
        )
        self._samples.append(sample)
        self._total_samples += 1

        if self._last_monotonic is None:
            self._last_monotonic = monotonic_at
        elif monotonic_at >= self._last_monotonic:
            self._active_duration += monotonic_at - self._last_monotonic
            self._last_monotonic = monotonic_at
        # A snapshot may have started just before Resume was clicked and finish
        # just after it. Do not move the active timer backwards for that frame.

        if sample.cpu_percent is not None:
            cpu = float(sample.cpu_percent)
            self._cpu_total += cpu
            self._cpu_count += 1
            self._cpu_peak = cpu if self._cpu_peak is None else max(self._cpu_peak, cpu)
        if sample.memory_percent is not None:
            memory = float(sample.memory_percent)
            self._memory_total += memory
            self._memory_count += 1
            self._memory_peak = (
                memory if self._memory_peak is None else max(self._memory_peak, memory)
            )
        self._alert_samples += bool(sample.alert)
        return sample

    def summary(self) -> dict[str, float | int | None]:
        return {
            "samples": self._total_samples,
            "retained_samples": len(self._samples),
            "duration_seconds": self._active_duration,
            "cpu_average": (
                round(self._cpu_total / self._cpu_count, 1) if self._cpu_count else None
            ),
            "cpu_peak": round(self._cpu_peak, 1) if self._cpu_peak is not None else None,
            "memory_average": (
                round(self._memory_total / self._memory_count, 1)
                if self._memory_count
                else None
            ),
            "memory_peak": (
                round(self._memory_peak, 1) if self._memory_peak is not None else None
            ),
            "alert_samples": self._alert_samples,
        }

    def export_csv(self, destination: str | Path) -> Path:
        if not self._samples:
            raise ValueError("There are no session samples to export.")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=self.fieldnames)
            writer.writeheader()
            for sample in self._samples:
                row = asdict(sample)
                row["timestamp"] = datetime.fromtimestamp(sample.timestamp).isoformat(timespec="seconds")
                writer.writerow(row)
        return path
