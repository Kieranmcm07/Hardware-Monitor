from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import fmean


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
    alert: str


class SessionRecorder:
    """Collects monitor snapshots for session statistics and CSV export."""

    fieldnames = tuple(TelemetrySample.__dataclass_fields__)

    def __init__(self) -> None:
        self._samples: list[TelemetrySample] = []
        self.active = True

    @property
    def samples(self) -> tuple[TelemetrySample, ...]:
        return tuple(self._samples)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def pause(self) -> None:
        self.active = False

    def resume(self) -> None:
        self.active = True

    def reset(self) -> None:
        self._samples.clear()
        self.active = True

    def capture(self, snapshot) -> TelemetrySample | None:
        if not self.active:
            return None
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
        sample = TelemetrySample(
            timestamp=float(snapshot.captured_at),
            cpu_percent=snapshot.cpu_usage_percent,
            memory_percent=snapshot.memory_used_percent,
            memory_used_gib=snapshot.memory_used_gib,
            system_drive=snapshot.system_drive,
            storage_used_percent=snapshot.disk_used_percent,
            storage_free_gib=snapshot.disk_free_gib,
            fixed_drives=drive_summary,
            alert="; ".join(alerts),
        )
        self._samples.append(sample)
        return sample

    def summary(self) -> dict[str, float | int | None]:
        cpu = [sample.cpu_percent for sample in self._samples if sample.cpu_percent is not None]
        memory = [sample.memory_percent for sample in self._samples if sample.memory_percent is not None]
        alerts = sum(bool(sample.alert) for sample in self._samples)
        duration = 0.0
        if len(self._samples) > 1:
            duration = self._samples[-1].timestamp - self._samples[0].timestamp
        return {
            "samples": len(self._samples),
            "duration_seconds": max(0.0, duration),
            "cpu_average": round(fmean(cpu), 1) if cpu else None,
            "cpu_peak": round(max(cpu), 1) if cpu else None,
            "memory_average": round(fmean(memory), 1) if memory else None,
            "memory_peak": round(max(memory), 1) if memory else None,
            "alert_samples": alerts,
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
