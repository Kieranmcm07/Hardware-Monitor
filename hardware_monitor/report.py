"""Privacy-conscious offline JSON and HTML hardware reports."""

from __future__ import annotations

import html
import json
import os
import platform
import tempfile
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


AUTHOR = "Kieranmcm07"


def _value(source: object, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, Mapping) else getattr(source, name, default)


def _safe_number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _enum_text(value: Any, default: str = "") -> str:
    """Serialize Enum-like values by their public value, not their class repr."""
    if value is None:
        return default
    return str(getattr(value, "value", value))


def build_report(
    snapshot: object,
    *,
    hardware: object | None = None,
    sensors: Iterable[object] = (),
    drive_health: Iterable[object] = (),
    benchmarks: Iterable[object] = (),
) -> dict[str, Any]:
    """Build an explicit allow-listed report that excludes usernames, serials and IPs."""
    drives = []
    for index, drive in enumerate(_value(snapshot, "drives", ()) or (), start=1):
        raw_name = str(_value(drive, "name", ""))
        # Drive letters and the Unix root reveal no user path. Other mount
        # points are deliberately anonymised in an export.
        safe_name = (
            raw_name
            if raw_name == "/" or (
                len(raw_name) == 2 and raw_name[0].isalpha() and raw_name[1] == ":"
            )
            else f"Volume {index}"
        )
        drives.append({
            "name": safe_name,
            "total_gib": _safe_number(_value(drive, "total_gib")),
            "free_gib": _safe_number(_value(drive, "free_gib")),
            "used_percent": _safe_number(_value(drive, "used_percent")),
        })
    sensor_rows = []
    for sensor in sensors:
        sensor_rows.append({
            "label": str(_value(sensor, "label", "Sensor")),
            "kind": _enum_text(_value(sensor, "kind", "")),
            "value": _safe_number(_value(sensor, "value")),
            "unit": str(_value(sensor, "unit", "")),
        })
    health_rows = []
    for drive in drive_health:
        # Model/health metrics are useful. Device paths and serial numbers are not.
        health_rows.append({
            "model": str(_value(drive, "model", "Drive")),
            "status": _enum_text(
                _value(drive, "status", _value(drive, "health", "unknown")),
                "unknown",
            ),
            "temperature_c": _safe_number(_value(drive, "temperature_c")),
            "power_on_hours": _safe_number(_value(drive, "power_on_hours")),
            "percentage_used": _safe_number(_value(drive, "percentage_used")),
        })
    benchmark_rows = []
    for result in benchmarks:
        benchmark_rows.append({
            "name": str(_value(result, "name", "Benchmark")),
            "status": str(_value(result, "status", "unknown")),
            "score": _safe_number(_value(result, "score")),
            "unit": str(_value(result, "unit", "")),
            "duration_seconds": _safe_number(_value(result, "duration_seconds")),
        })
    return {
        "report": {
            "format_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "application": "NEXUS Hardware Monitor",
            "author": AUTHOR,
            "privacy": "Local report; usernames, IP addresses, device paths and serial numbers excluded.",
        },
        "system": {
            "operating_system": str(_value(snapshot, "operating_system", platform.platform())),
            "processor": str(_value(snapshot, "processor", "Unavailable")),
            "physical_cores": _safe_number(_value(snapshot, "physical_cores")),
            "logical_cpus": _safe_number(_value(snapshot, "logical_cpus")),
            "memory_installed_gib": _safe_number(_value(snapshot, "memory_installed_gib")),
            "motherboard": str(_value(hardware, "motherboard", "Unavailable")),
            "bios_version": str(_value(hardware, "bios_version", "Unavailable")),
            "gpu_names": [str(value) for value in (_value(hardware, "gpu_names", ()) or ())],
        },
        "telemetry": {
            "cpu_percent": _safe_number(_value(snapshot, "cpu_usage_percent")),
            "memory_percent": _safe_number(_value(snapshot, "memory_used_percent")),
            "storage_percent": _safe_number(_value(snapshot, "disk_used_percent")),
            "battery_percent": _safe_number(_value(snapshot, "battery_percent")),
            "uptime_seconds": _safe_number(_value(snapshot, "uptime_seconds")),
        },
        "drives": drives,
        "sensors": sensor_rows,
        "drive_health": health_rows,
        "benchmarks": benchmark_rows,
    }


def report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def report_html(report: Mapping[str, Any]) -> str:
    """Render a standalone report. Every dynamic value is HTML-escaped."""
    def esc(value: Any) -> str:
        return html.escape("—" if value is None else str(value), quote=True)

    def rows(section: Mapping[str, Any]) -> str:
        return "".join(
            f"<tr><th>{esc(key.replace('_', ' ').title())}</th><td>{esc(', '.join(value) if isinstance(value, list) else value)}</td></tr>"
            for key, value in section.items()
        )

    cards = []
    for title, key in (("System", "system"), ("Live telemetry", "telemetry")):
        section = report.get(key, {})
        if isinstance(section, Mapping):
            cards.append(f"<section><h2>{esc(title)}</h2><table>{rows(section)}</table></section>")
    for title, key in (("Drives", "drives"), ("Sensors", "sensors"),
                       ("Drive health", "drive_health"), ("Benchmarks", "benchmarks")):
        items = report.get(key, [])
        if isinstance(items, list) and items:
            body = "".join(f"<table>{rows(item)}</table>" for item in items if isinstance(item, Mapping))
            cards.append(f"<section><h2>{esc(title)}</h2>{body}</section>")
    metadata = report.get("report", {})
    created = metadata.get("created_utc", "") if isinstance(metadata, Mapping) else ""
    return """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>NEXUS Hardware Report</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#080808;color:#f5f5f5;font:15px system-ui,sans-serif}main{max-width:1000px;margin:auto;padding:40px 20px}h1{letter-spacing:.08em}h1 span,h2{color:#ef3340}section{background:#121212;border:2px solid #333;border-radius:18px;padding:18px;margin:16px 0}table{width:100%;border-collapse:collapse;margin:8px 0}th,td{padding:9px;text-align:left;border-bottom:1px solid #2b2b2b}th{width:36%;color:#aaa}footer{color:#888;margin-top:24px}</style></head><body><main>
<h1>NEXUS <span>// HARDWARE REPORT</span></h1><p>Private, offline system summary.</p>""" + "".join(cards) + f"<footer>Made by {esc(AUTHOR)} · {esc(created)}</footer></main></body></html>\n"


def write_report(path: str | Path, report: Mapping[str, Any], format: str | None = None) -> Path:
    """Atomically write JSON or HTML selected by format or file suffix."""
    destination = Path(path)
    selected = (format or destination.suffix.removeprefix(".") or "json").lower()
    if selected not in {"json", "html", "htm"}:
        raise ValueError("report format must be json or html")
    content = report_json(report) if selected == "json" else report_html(report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         delete=False, suffix=".tmp") as handle:
            temporary = Path(handle.name); handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
